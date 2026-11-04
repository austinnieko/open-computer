"""Multi-agent coordination records: tasks, dependencies, claims, conflicts, messages.

Durable state for the coordinator. Every task is scoped to a repository by
``repo_key`` (the realpath of ``git rev-parse --git-common-dir``), which is
shared by the main checkout and all of its worktrees.

Exclusive resources (a branch, a worktree directory, a chat binding) are
claimed through ``coord_claims``. Its UNIQUE ``(repo_key, resource)``
constraint is the cross-agent lock: two agents racing for the same resource
cannot both commit a claim, and the lock survives restarts.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    Text,
    UniqueConstraint,
    delete,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.exc import IntegrityError

from cptr.models.base import Base
from cptr.utils.config import now_ms
from cptr.utils.db import get_db

TASK_STATUSES = (
    "queued",
    "running",
    "blocked",
    "paused",
    "reviewing",
    "integration_ready",
    "completed",
    "failed",
    "ended",
)
CONFLICT_LEVELS = ("info", "potential", "confirmed", "blocking")
TERMINAL_STATUSES = frozenset({"completed", "failed", "ended"})
ACTIVE_STATUSES = frozenset(set(TASK_STATUSES) - TERMINAL_STATUSES)


def _uuid() -> str:
    return str(uuid.uuid4())


def _task_id() -> str:
    return f"task_{uuid.uuid4().hex[:10]}"


class ClaimConflict(Exception):
    """An exclusive resource is already claimed by another task."""

    def __init__(self, resource: str, owner_task_id: str):
        super().__init__(f"{resource} is already claimed by {owner_task_id}")
        self.resource = resource
        self.owner_task_id = owner_task_id


class CoordTask(Base):
    __tablename__ = "coord_tasks"

    id = Column(Text, primary_key=True, default=_task_id)
    user_id = Column(Text, nullable=False)
    repo_key = Column(Text, nullable=False)
    repo_root = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    title_key = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="queued")
    chat_id = Column(Text, nullable=True)
    agent = Column(Text, nullable=True)
    agent_session_id = Column(Text, nullable=True)
    branch = Column(Text, nullable=True)
    base_ref = Column(Text, nullable=True)
    base_sha = Column(Text, nullable=True)
    worktree_path = Column(Text, nullable=True)
    pr_number = Column(BigInteger, nullable=True)
    pr_url = Column(Text, nullable=True)
    pr_state = Column(Text, nullable=True)
    files = Column(JSON, nullable=True)  # declared + observed paths
    validation = Column(JSON, nullable=True)
    blockers = Column(JSON, nullable=True)  # [{kind, detail, since}]
    meta = Column(JSON, nullable=True)  # milestones, error, etc.
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_coord_tasks_repo_status", "repo_key", "status"),
        Index("ix_coord_tasks_chat", "chat_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "repo_key": self.repo_key,
            "repo_root": self.repo_root,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "chat_id": self.chat_id,
            "agent": self.agent,
            "agent_session_id": self.agent_session_id,
            "branch": self.branch,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "worktree_path": self.worktree_path,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "pr_state": self.pr_state,
            "files": self.files or [],
            "validation": self.validation,
            "blockers": self.blockers or [],
            "meta": self.meta or {},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    # ── Queries ──────────────────────────────────────────────

    @staticmethod
    async def get_by_id(task_id: str) -> CoordTask | None:
        async with await get_db() as db:
            result = await db.execute(select(CoordTask).where(CoordTask.id == task_id))
            return result.scalar_one_or_none()

    @staticmethod
    async def get_by_chat(chat_id: str) -> CoordTask | None:
        async with await get_db() as db:
            result = await db.execute(
                select(CoordTask)
                .where(CoordTask.chat_id == chat_id)
                .order_by(CoordTask.created_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    @staticmethod
    async def query(
        *,
        user_id: str | None = None,
        repo_key: str | None = None,
        statuses: set[str] | frozenset[str] | None = None,
    ) -> list[CoordTask]:
        async with await get_db() as db:
            stmt = select(CoordTask)
            if user_id is not None:
                stmt = stmt.where(CoordTask.user_id == user_id)
            if repo_key is not None:
                stmt = stmt.where(CoordTask.repo_key == repo_key)
            if statuses:
                stmt = stmt.where(CoordTask.status.in_(sorted(statuses)))
            result = await db.execute(stmt.order_by(CoordTask.created_at))
            return list(result.scalars().all())

    @staticmethod
    async def find_reusable(
        repo_key: str,
        *,
        chat_id: str | None,
        branch: str | None,
        title_key: str,
    ) -> CoordTask | None:
        """Return an active task that a new registration should reuse.

        Matches, in order of strength: the same chat, the same branch, then the
        same normalised title.
        """
        tasks = await CoordTask.query(repo_key=repo_key, statuses=ACTIVE_STATUSES)
        for match in (
            lambda t: chat_id and t.chat_id == chat_id,
            lambda t: branch and t.branch == branch,
            lambda t: t.title_key == title_key,
        ):
            for task in tasks:
                if match(task):
                    return task
        return None

    # ── Writes ───────────────────────────────────────────────

    @staticmethod
    async def create_with_claims(*, claims: list[tuple[str, str]], **fields) -> CoordTask:
        """Insert a task and its exclusive claims in one transaction.

        ``claims`` is a list of ``(kind, resource)``. If any resource is
        already claimed, nothing is written and ``ClaimConflict`` is raised.
        """
        now = now_ms()
        task = CoordTask(
            id=fields.pop("id", None) or _task_id(),
            created_at=now,
            updated_at=now,
            **fields,
        )
        async with await get_db() as db:
            db.add(task)
            for kind, resource in claims:
                db.add(
                    CoordClaim(
                        repo_key=task.repo_key,
                        resource=resource,
                        kind=kind,
                        task_id=task.id,
                        created_at=now,
                    )
                )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                owner = await _first_claimed(task.repo_key, [r for _, r in claims])
                if owner:
                    raise ClaimConflict(*owner) from None
                raise
        return task

    @staticmethod
    async def update_fields(task_id: str, *, expect_status: str | None = None, **fields) -> bool:
        """Update columns. With ``expect_status`` this is a compare-and-set."""
        fields["updated_at"] = now_ms()
        async with await get_db() as db:
            stmt = update(CoordTask).where(CoordTask.id == task_id)
            if expect_status is not None:
                stmt = stmt.where(CoordTask.status == expect_status)
            result = await db.execute(stmt.values(**fields))
            await db.commit()
            return result.rowcount > 0

    @staticmethod
    async def delete_with_records(task_id: str) -> None:
        """Delete a task and everything that refers to it, in one transaction.

        Removes its dependency edges (both directions), claims, conflicts and
        messages. Integration runs keep their history.
        """
        async with await get_db() as db:
            await db.execute(
                delete(CoordTaskDep).where(
                    (CoordTaskDep.task_id == task_id) | (CoordTaskDep.depends_on_id == task_id)
                )
            )
            await db.execute(delete(CoordClaim).where(CoordClaim.task_id == task_id))
            await db.execute(
                delete(CoordConflict).where(
                    (CoordConflict.task_a == task_id) | (CoordConflict.task_b == task_id)
                )
            )
            await db.execute(
                delete(CoordMessage).where(
                    (CoordMessage.from_task == task_id) | (CoordMessage.to_task == task_id)
                )
            )
            await db.execute(delete(CoordTask).where(CoordTask.id == task_id))
            await db.commit()


async def _first_claimed(repo_key: str, resources: list[str]) -> tuple[str, str] | None:
    async with await get_db() as db:
        result = await db.execute(
            select(CoordClaim).where(
                CoordClaim.repo_key == repo_key, CoordClaim.resource.in_(resources)
            )
        )
        row = result.scalars().first()
        return (row.resource, row.task_id) if row else None


class CoordTaskDep(Base):
    __tablename__ = "coord_task_deps"

    task_id = Column(Text, primary_key=True)
    depends_on_id = Column(Text, primary_key=True)
    created_at = Column(BigInteger, nullable=False)

    @staticmethod
    async def add(task_id: str, depends_on_id: str) -> None:
        async with await get_db() as db:
            db.add(CoordTaskDep(task_id=task_id, depends_on_id=depends_on_id, created_at=now_ms()))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()  # already present

    @staticmethod
    async def remove(task_id: str, depends_on_id: str) -> bool:
        async with await get_db() as db:
            result = await db.execute(
                delete(CoordTaskDep).where(
                    CoordTaskDep.task_id == task_id, CoordTaskDep.depends_on_id == depends_on_id
                )
            )
            await db.commit()
            return result.rowcount > 0

    @staticmethod
    async def all_for_repo(repo_key: str) -> list[tuple[str, str]]:
        async with await get_db() as db:
            result = await db.execute(
                select(CoordTaskDep.task_id, CoordTaskDep.depends_on_id)
                .join(CoordTask, CoordTask.id == CoordTaskDep.task_id)
                .where(CoordTask.repo_key == repo_key)
            )
            return [(a, b) for a, b in result.all()]


class CoordClaim(Base):
    __tablename__ = "coord_claims"

    id = Column(Text, primary_key=True, default=_uuid)
    repo_key = Column(Text, nullable=False)
    resource = Column(Text, nullable=False)  # e.g. "branch:feat/x", "worktree:/abs/path"
    kind = Column(Text, nullable=False)
    task_id = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("repo_key", "resource", name="uq_coord_claim_resource"),
        Index("ix_coord_claims_task", "task_id"),
    )

    @staticmethod
    async def claim(repo_key: str, task_id: str, kind: str, resource: str) -> None:
        """Claim one resource. Idempotent for the same task; raises ClaimConflict otherwise."""
        async with await get_db() as db:
            db.add(
                CoordClaim(
                    repo_key=repo_key,
                    resource=resource,
                    kind=kind,
                    task_id=task_id,
                    created_at=now_ms(),
                )
            )
            try:
                await db.commit()
                return
            except IntegrityError:
                await db.rollback()
        owner = await _first_claimed(repo_key, [resource])
        if owner and owner[1] != task_id:
            raise ClaimConflict(*owner)

    @staticmethod
    async def release(task_id: str, *, kinds: set[str] | None = None) -> int:
        async with await get_db() as db:
            stmt = delete(CoordClaim).where(CoordClaim.task_id == task_id)
            if kinds:
                stmt = stmt.where(CoordClaim.kind.in_(sorted(kinds)))
            result = await db.execute(stmt)
            await db.commit()
            return result.rowcount

    @staticmethod
    async def for_task(task_id: str) -> list[CoordClaim]:
        async with await get_db() as db:
            result = await db.execute(select(CoordClaim).where(CoordClaim.task_id == task_id))
            return list(result.scalars().all())


class CoordConflict(Base):
    """A detected conflict between two tasks. Populated by the Phase 2 detector."""

    __tablename__ = "coord_conflicts"

    id = Column(Text, primary_key=True, default=_uuid)
    repo_key = Column(Text, nullable=False)
    task_a = Column(Text, nullable=False)
    task_b = Column(Text, nullable=True)
    level = Column(Text, nullable=False)  # info | potential | confirmed | blocking
    kind = Column(Text, nullable=False)
    evidence = Column(JSON, nullable=True)
    status = Column(Text, nullable=False, default="open")
    fingerprint = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_coord_conflict_fingerprint"),
        Index("ix_coord_conflicts_repo_status", "repo_key", "status"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo_key": self.repo_key,
            "task_a": self.task_a,
            "task_b": self.task_b,
            "level": self.level,
            "kind": self.kind,
            "evidence": self.evidence or {},
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    async def query(
        repo_key: str, *, statuses: set[str] | None = None, task_id: str | None = None
    ) -> list[CoordConflict]:
        async with await get_db() as db:
            stmt = select(CoordConflict).where(CoordConflict.repo_key == repo_key)
            if statuses:
                stmt = stmt.where(CoordConflict.status.in_(sorted(statuses)))
            if task_id:
                stmt = stmt.where(
                    (CoordConflict.task_a == task_id) | (CoordConflict.task_b == task_id)
                )
            result = await db.execute(stmt.order_by(CoordConflict.created_at))
            return list(result.scalars().all())

    @staticmethod
    async def upsert(
        *,
        repo_key: str,
        task_a: str,
        task_b: str | None,
        level: str,
        kind: str,
        evidence: dict,
        fingerprint: str,
    ) -> tuple[CoordConflict, str]:
        """Insert or refresh a conflict. Returns ``(row, change)``.

        ``change`` is ``"new"``, ``"escalated"``, ``"reopened"``, ``"updated"``
        or ``"unchanged"`` so callers only notify on meaningful changes.
        """
        now = now_ms()
        async with await get_db() as db:
            result = await db.execute(
                select(CoordConflict).where(CoordConflict.fingerprint == fingerprint)
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = CoordConflict(
                    repo_key=repo_key,
                    task_a=task_a,
                    task_b=task_b,
                    level=level,
                    kind=kind,
                    evidence=evidence,
                    status="open",
                    fingerprint=fingerprint,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                change = "new"
            else:
                escalated = CONFLICT_LEVELS.index(level) > CONFLICT_LEVELS.index(row.level)
                if row.status == "resolved":
                    change = "reopened"
                    row.status = "open"
                elif escalated:
                    change = "escalated"
                    row.status = "open"  # an acknowledged conflict that got worse needs a look
                elif row.level != level or row.evidence != evidence:
                    change = "updated"
                else:
                    change = "unchanged"
                if change != "unchanged":
                    row.level, row.evidence, row.updated_at = level, evidence, now
            try:
                await db.commit()
            except IntegrityError:  # raced with another scan inserting the same fingerprint
                await db.rollback()
                return await CoordConflict.upsert(
                    repo_key=repo_key,
                    task_a=task_a,
                    task_b=task_b,
                    level=level,
                    kind=kind,
                    evidence=evidence,
                    fingerprint=fingerprint,
                )
            return row, change

    @staticmethod
    async def query_by_id(conflict_id: str) -> list[CoordConflict]:
        async with await get_db() as db:
            result = await db.execute(select(CoordConflict).where(CoordConflict.id == conflict_id))
            return list(result.scalars().all())

    @staticmethod
    async def set_status(conflict_id: str, status: str) -> bool:
        async with await get_db() as db:
            result = await db.execute(
                update(CoordConflict)
                .where(CoordConflict.id == conflict_id)
                .values(status=status, updated_at=now_ms())
            )
            await db.commit()
            return result.rowcount > 0


class CoordMessage(Base):
    """A structured coordination message to one task.

    Messages are fanned out to concrete recipients when posted, so
    ``to_task`` is set on every row and delivery is tracked per recipient.
    """

    __tablename__ = "coord_messages"

    id = Column(Text, primary_key=True, default=_uuid)
    repo_key = Column(Text, nullable=False)
    from_task = Column(Text, nullable=True)
    to_task = Column(Text, nullable=True)
    kind = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    payload = Column(JSON, nullable=True)
    delivered_at = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_coord_messages_to", "to_task", "delivered_at"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_task": self.from_task,
            "to_task": self.to_task,
            "kind": self.kind,
            "body": self.body,
            "payload": self.payload or {},
            "delivered_at": self.delivered_at,
            "created_at": self.created_at,
        }

    @staticmethod
    async def create(**fields) -> CoordMessage:
        row = CoordMessage(created_at=now_ms(), **fields)
        async with await get_db() as db:
            db.add(row)
            await db.commit()
        return row

    @staticmethod
    async def pending_for(task_id: str) -> list[CoordMessage]:
        async with await get_db() as db:
            result = await db.execute(
                select(CoordMessage)
                .where(CoordMessage.to_task == task_id, CoordMessage.delivered_at.is_(None))
                .order_by(CoordMessage.created_at)
            )
            return list(result.scalars().all())

    @staticmethod
    async def mark_delivered(message_ids: list[str]) -> set[str]:
        """Mark messages delivered and return the ids this call claimed.

        Only undelivered rows change, so two concurrent deliveries can never
        both hand the same message to an agent.
        """
        if not message_ids:
            return set()
        async with await get_db() as db:
            result = await db.execute(
                update(CoordMessage)
                .where(CoordMessage.id.in_(message_ids), CoordMessage.delivered_at.is_(None))
                .values(delivered_at=now_ms())
                .returning(CoordMessage.id)
            )
            claimed = {row[0] for row in result.all()}
            await db.commit()
            return claimed

    @staticmethod
    async def for_repo(repo_key: str, *, task_id: str | None = None, limit: int = 200):
        async with await get_db() as db:
            stmt = select(CoordMessage).where(CoordMessage.repo_key == repo_key)
            if task_id:
                stmt = stmt.where(
                    (CoordMessage.to_task == task_id) | (CoordMessage.from_task == task_id)
                )
            result = await db.execute(stmt.order_by(CoordMessage.created_at.desc()).limit(limit))
            return list(result.scalars().all())


class CoordIntegration(Base):
    """One coordinator integration run: task branches merged in order and tested.

    The integration branch and worktree are kept afterwards for traceability.
    Nothing here ever merges into the base branch or a GitHub PR.
    """

    __tablename__ = "coord_integrations"

    id = Column(Text, primary_key=True, default=lambda: f"int_{uuid.uuid4().hex[:10]}")
    user_id = Column(Text, nullable=False)
    repo_key = Column(Text, nullable=False)
    status = Column(Text, nullable=False)  # running | passed | failed | interrupted
    base_branch = Column(Text, nullable=True)
    base_sha = Column(Text, nullable=True)
    branch = Column(Text, nullable=True)
    worktree_path = Column(Text, nullable=True)
    task_order = Column(JSON, nullable=True)  # [task_id]
    steps = Column(JSON, nullable=True)  # [{task_id, result, commit?, files?, reason?}]
    tests = Column(JSON, nullable=True)  # {command, exit_code, output_tail, attributed_to?}
    summary = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_coord_integrations_repo", "repo_key", "created_at"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo_key": self.repo_key,
            "status": self.status,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "task_order": self.task_order or [],
            "steps": self.steps or [],
            "tests": self.tests,
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    async def create(**fields) -> CoordIntegration:
        now = now_ms()
        row = CoordIntegration(created_at=now, updated_at=now, **fields)
        async with await get_db() as db:
            db.add(row)
            await db.commit()
        return row

    @staticmethod
    async def get_by_id(run_id: str) -> CoordIntegration | None:
        async with await get_db() as db:
            result = await db.execute(select(CoordIntegration).where(CoordIntegration.id == run_id))
            return result.scalar_one_or_none()

    @staticmethod
    async def query(repo_key: str | None = None, *, status: str | None = None, limit: int = 50):
        async with await get_db() as db:
            stmt = select(CoordIntegration)
            if repo_key:
                stmt = stmt.where(CoordIntegration.repo_key == repo_key)
            if status:
                stmt = stmt.where(CoordIntegration.status == status)
            result = await db.execute(
                stmt.order_by(CoordIntegration.created_at.desc()).limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def update_fields(run_id: str, **fields) -> None:
        fields["updated_at"] = now_ms()
        async with await get_db() as db:
            await db.execute(
                update(CoordIntegration).where(CoordIntegration.id == run_id).values(**fields)
            )
            await db.commit()
