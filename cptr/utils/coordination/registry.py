"""Coordinator task registry: registration, reuse, session binding, status, recovery.

Concurrency model (single process, like the rest of the app):

* An ``asyncio.Lock`` per repository serialises read-modify-write flows such
  as "find reusable task, else create one".
* Exclusive resources are additionally claimed in ``coord_claims`` whose UNIQUE
  constraint makes a double claim impossible even if two code paths race or
  the process restarts in the middle of a flow.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from typing import Any

from cptr.models.chats import Chat
from cptr.models.coordination import (
    ACTIVE_STATUSES,
    TASK_STATUSES,
    ClaimConflict,
    CoordClaim,
    CoordTask,
    CoordTaskDep,
)
from cptr.utils.config import now_ms
from cptr.utils.coordination import worktrees as wt
from cptr.utils.identity import ExecutionIdentity

logger = logging.getLogger(__name__)

_repo_locks: dict[str, asyncio.Lock] = {}

# Allowed status transitions. Anything else is rejected.
_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "blocked", "paused", "failed"},
    "running": {"blocked", "paused", "reviewing", "integration_ready", "failed", "completed"},
    "blocked": {"queued", "running", "paused", "failed"},
    "paused": {"queued", "running", "blocked", "failed"},
    "reviewing": {"running", "blocked", "integration_ready", "failed"},
    "integration_ready": {"running", "reviewing", "blocked", "completed", "failed"},
    "failed": {"queued"},  # explicit retry
    "completed": set(),
}


class CoordinationError(Exception):
    pass


def repo_lock(repo_key: str) -> asyncio.Lock:
    return _repo_locks.setdefault(repo_key, asyncio.Lock())


def title_key(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", title.lower())).strip()


def _blocker(kind: str, detail: str) -> dict[str, Any]:
    return {"kind": kind, "detail": detail, "since": now_ms()}


def _milestone(meta: dict | None, name: str, **data) -> dict:
    meta = dict(meta or {})
    milestones = dict(meta.get("milestones") or {})
    milestones[name] = {"at": now_ms(), **data}
    meta["milestones"] = milestones
    return meta


async def register_task(
    *,
    user_id: str,
    repo_path: str,
    title: str,
    description: str | None = None,
    branch: str | None = None,
    base_ref: str | None = None,
    chat_id: str | None = None,
    agent: str | None = None,
    depends_on: list[str] | None = None,
    files: list[str] | None = None,
    isolate: bool = True,
    start_prompt: str | None = None,
    model_id: str | None = None,
    identity: ExecutionIdentity | None = None,
) -> dict[str, Any]:
    """Register a coding task, reusing an existing one when it is the same work.

    Returns ``{"task": dict, "reused": bool, "worktree": dict | None}``.
    With ``isolate=True`` (the default) a dedicated branch-backed worktree is
    created or reused. With ``isolate=False`` the task is registered as
    read-only work in the main checkout and claims no branch.
    """
    title = (title or "").strip()
    if not title:
        raise CoordinationError("Task title is required")
    repo = await wt.repo_identity(repo_path, identity)
    tkey = title_key(title)

    async with repo_lock(repo.key):
        existing = await CoordTask.find_reusable(
            repo.key, chat_id=chat_id, branch=branch, title_key=tkey
        )
        if existing:
            worktree = None
            if existing.branch and existing.worktree_path:
                worktree = await _ensure_task_worktree(existing, repo, identity)
            refreshed = await CoordTask.get_by_id(existing.id)
            return {"task": refreshed.to_dict(), "reused": True, "worktree": worktree}

        if chat_id and not await Chat.get_by_id(chat_id):
            raise CoordinationError(f"Unknown chat {chat_id}")
        for dep in depends_on or []:
            dep_task = await CoordTask.get_by_id(dep)
            if not dep_task or dep_task.repo_key != repo.key:
                raise CoordinationError(f"Unknown dependency {dep!r} for this repository")

        task_id = f"task_{uuid.uuid4().hex[:10]}"
        claims: list[tuple[str, str]] = []
        worktree_path = None
        resolved_base = None
        if isolate:
            branch = await wt.validate_branch_name(
                repo.main_root,
                branch or f"task/{wt.slugify(title, max_len=32)}-{task_id[5:11]}",
                identity,
            )
            worktree_path = wt.default_worktree_path(
                repo.main_root, f"{wt.slugify(title, max_len=32)}-{task_id[5:11]}"
            )
            base_branch = await _base_branch(repo.main_root, base_ref, identity)
            base_ref = base_ref or "HEAD"
            resolved_base = await wt.resolve_sha(repo.main_root, base_ref, identity)
            if resolved_base is None:
                raise CoordinationError(f"Unknown base ref {base_ref!r}")
            claims += [("branch", f"branch:{branch}"), ("worktree", f"worktree:{worktree_path}")]
        else:
            branch = None
            base_branch = None
        if chat_id:
            claims.append(("chat", f"chat:{chat_id}"))
        meta: dict[str, Any] = {"isolated": isolate}
        if base_branch:
            meta["base_branch"] = base_branch
        if start_prompt:
            if not chat_id:
                raise CoordinationError("A start prompt needs a chat to run in")
            meta["start"] = {"prompt": start_prompt, "model_id": model_id}

        try:
            task = await CoordTask.create_with_claims(
                id=task_id,
                claims=claims,
                user_id=user_id,
                repo_key=repo.key,
                repo_root=repo.main_root,
                title=title,
                title_key=tkey,
                description=description,
                status="queued",
                chat_id=chat_id,
                agent=agent,
                branch=branch,
                base_ref=base_ref if isolate else None,
                base_sha=resolved_base,
                worktree_path=worktree_path,
                files=sorted(set(files or [])),
                blockers=[],
                meta=_milestone(meta, "registered"),
            )
        except ClaimConflict as exc:
            raise CoordinationError(str(exc)) from exc

        for dep in depends_on or []:
            await CoordTaskDep.add(task.id, dep)

        worktree = await _ensure_task_worktree(task, repo, identity) if isolate else None
        if chat_id:
            await bind_chat(task.id, chat_id)
        refreshed = await CoordTask.get_by_id(task.id)
        return {"task": refreshed.to_dict(), "reused": False, "worktree": worktree}


async def _base_branch(
    repo_root: str, base_ref: str | None, identity: ExecutionIdentity | None
) -> str | None:
    """The local branch a task integrates into: ``base_ref`` if it names one,
    else the branch checked out in the main checkout."""
    from cptr.utils.git import _run

    if base_ref and base_ref != "HEAD":
        return base_ref if await wt.branch_exists(repo_root, base_ref, identity) else None
    code, out, _ = await _run(
        "symbolic-ref", "--quiet", "--short", "HEAD", cwd=repo_root, check=False, identity=identity
    )
    return out.strip() if code == 0 and out.strip() else None


async def _ensure_task_worktree(
    task: CoordTask, repo: wt.RepoIdentity, identity: ExecutionIdentity | None
) -> dict[str, Any] | None:
    """Create/reuse the task's worktree; on failure mark the task blocked, never raise."""
    try:
        result = await wt.ensure_worktree(
            repo,
            branch=task.branch,
            path=task.worktree_path,
            base_ref=task.base_sha or task.base_ref,
            identity=identity,
        )
    except wt.WorktreeError as exc:
        logger.warning("[coord] worktree for %s failed: %s", task.id, exc)
        await CoordTask.update_fields(
            task.id,
            status="blocked" if task.status != "failed" else task.status,
            blockers=[*(task.blockers or []), _blocker("worktree", str(exc))],
        )
        return {"error": str(exc)}
    fields: dict[str, Any] = {}
    if os.path.realpath(result["path"]) != os.path.realpath(task.worktree_path or ""):
        fields["worktree_path"] = result["path"]
        await CoordClaim.claim(task.repo_key, task.id, "worktree", f"worktree:{result['path']}")
    if "worktree_ready" not in ((task.meta or {}).get("milestones") or {}):
        fields["meta"] = _milestone(task.meta, "worktree_ready", path=result["path"])
    remaining = [b for b in (task.blockers or []) if b.get("kind") != "worktree"]
    if remaining != (task.blockers or []):
        fields["blockers"] = remaining
        if task.status == "blocked" and not remaining:
            fields["status"] = "queued"
    if fields:
        await CoordTask.update_fields(task.id, **fields)
    return result


async def bind_chat(task_id: str, chat_id: str) -> dict[str, Any]:
    """Bind a chat (agent conversation) to a task so it runs inside the task worktree.

    Sets ``chat.meta.cwd`` and ``chat.meta.coord_task_id``. The chat keeps its
    ``workspace`` (the main checkout) so it stays listed under the project.
    """
    task = await CoordTask.get_by_id(task_id)
    if not task:
        raise CoordinationError(f"Unknown task {task_id}")
    chat = await Chat.get_by_id(chat_id)
    if not chat:
        raise CoordinationError(f"Unknown chat {chat_id}")
    if task.chat_id and task.chat_id != chat_id:
        raise CoordinationError(f"{task_id} is already bound to chat {task.chat_id}")
    try:
        await CoordClaim.claim(task.repo_key, task.id, "chat", f"chat:{chat_id}")
    except ClaimConflict as exc:
        raise CoordinationError(str(exc)) from exc

    meta = dict(chat.meta or {})
    meta["coord_task_id"] = task.id
    if task.worktree_path:
        meta["cwd"] = task.worktree_path
    else:
        meta.pop("cwd", None)
    await Chat.update_meta(chat_id, meta, now_ms())
    await CoordTask.update_fields(
        task.id, chat_id=chat_id, meta=_milestone(task.meta, "chat_bound", chat_id=chat_id)
    )
    return (await CoordTask.get_by_id(task.id)).to_dict()


async def resolve_chat_cwd(
    chat_id: str, chat_meta: dict | None, identity: ExecutionIdentity | None = None
) -> str | None:
    """Return the trusted task worktree a chat must run in, or ``None``.

    ``chat.meta.cwd`` alone is never trusted: it must match a registered task
    that is bound to this chat, and the directory must still be a worktree of
    the task's repository. Anything else falls back to the chat's workspace.
    """
    meta = chat_meta or {}
    cwd, task_id = meta.get("cwd"), meta.get("coord_task_id")
    if not cwd or not task_id:
        return None
    task = await CoordTask.get_by_id(task_id)
    if (
        not task
        or task.chat_id != chat_id
        or not task.worktree_path
        or os.path.realpath(cwd) != os.path.realpath(task.worktree_path)
        or not wt.is_within(task.worktree_path, wt.managed_root(task.repo_root))
    ):
        logger.warning("[coord] ignoring untrusted cwd %r for chat %s", cwd, chat_id)
        return None
    if not await wt.is_worktree_of(
        task.worktree_path, wt.RepoIdentity(task.repo_key, task.repo_root), identity
    ):
        logger.warning("[coord] worktree %s for %s is missing or foreign", cwd, task_id)
        return None
    return os.path.realpath(task.worktree_path)


async def set_status(
    task_id: str,
    status: str,
    *,
    reason: str | None = None,
    expect: str | None = None,
) -> dict[str, Any]:
    """Move a task to ``status`` if the transition is allowed (compare-and-set)."""
    if status not in TASK_STATUSES:
        raise CoordinationError(f"Unknown status {status!r}")
    task = await CoordTask.get_by_id(task_id)
    if not task:
        raise CoordinationError(f"Unknown task {task_id}")
    current = expect or task.status
    if task.status != current:
        raise CoordinationError(f"{task_id} is {task.status}, expected {current}")
    if status == current:
        return task.to_dict()
    if status not in _TRANSITIONS.get(current, set()):
        raise CoordinationError(f"Cannot move {task_id} from {current} to {status}")
    fields: dict[str, Any] = {"status": status}
    meta = dict(task.meta or {})
    if reason:
        meta["status_reason"] = reason
    fields["meta"] = _milestone(meta, status)
    if not await CoordTask.update_fields(task_id, expect_status=current, **fields):
        raise CoordinationError(f"{task_id} changed concurrently; retry")
    return (await CoordTask.get_by_id(task_id)).to_dict()


async def record_session(task_id: str, *, agent: str | None, session_id: str | None) -> None:
    """Persist the agent session that is working on a task (for recovery)."""
    task = await CoordTask.get_by_id(task_id)
    if not task or (task.agent_session_id == session_id and task.agent == agent):
        return
    await CoordTask.update_fields(task_id, agent=agent or task.agent, agent_session_id=session_id)


async def reconcile(
    *,
    active_chat_ids: set[str],
    identity_for_user=None,
) -> dict[str, int]:
    """Reconcile durable task state with reality after a restart or reconnect.

    * ``running`` tasks whose chat has no live turn become ``paused``.
    * A missing worktree becomes a ``worktree`` blocker (the task is not
      failed: the branch and its commits still exist).
    * Dirty worktrees are left exactly as they are.
    * Claims are never released here; that needs an explicit user action.
    """
    counts = {"paused": 0, "blocked": 0, "ok": 0}
    for task in await CoordTask.query(statuses=ACTIVE_STATUSES):
        identity = None
        if identity_for_user is not None:
            try:
                identity = await identity_for_user(task.user_id)
            except Exception:
                identity = None
        fields: dict[str, Any] = {}
        blockers = [b for b in (task.blockers or []) if b.get("kind") != "worktree_missing"]
        if task.worktree_path:
            state = await wt.worktree_state(task.worktree_path, identity)
            if not state.get("exists"):
                blockers.append(_blocker("worktree_missing", task.worktree_path))
            elif state.get("branch") and task.branch and state["branch"] != task.branch:
                blockers.append(
                    _blocker(
                        "worktree_missing",
                        f"{task.worktree_path} is on {state['branch']}, expected {task.branch}",
                    )
                )
        if blockers != (task.blockers or []):
            fields["blockers"] = blockers
        status = task.status
        if any(b["kind"] == "worktree_missing" for b in blockers) and status in {
            "queued",
            "running",
            "paused",
        }:
            status = "blocked"
        elif status == "running" and task.chat_id not in active_chat_ids:
            status = "paused"
        elif status == "blocked" and not blockers and task.blockers:
            # Every blocker was a missing worktree and it is back: resume manually.
            status = "paused"
        if status != task.status:
            fields["status"] = status
            fields["meta"] = _milestone(
                {**(task.meta or {}), "status_reason": "reconciled after restart"}, status
            )
        if fields:
            await CoordTask.update_fields(task.id, **fields)
            counts[
                "blocked" if status == "blocked" else "paused" if status == "paused" else "ok"
            ] += 1
        else:
            counts["ok"] += 1
    return counts
