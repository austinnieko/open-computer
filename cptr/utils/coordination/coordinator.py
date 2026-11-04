"""Coordinator: turns detection results into effects, and wires the triggers.

``refresh(repo_key)`` scans for conflicts, applies effects (messages,
blockers, unblocking), runs the scheduler and pushes ``coord:*`` events to
the UI. It is triggered by events (agent turn end, registration, status or
dependency changes) and coalesced per repository so a burst of events causes
at most one extra scan.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cptr.models.coordination import CONFLICT_LEVELS, CoordConflict, CoordTask, CoordTaskDep
from cptr.utils.config import now_ms
from cptr.utils.coordination import changes as ch
from cptr.utils.coordination import conflicts, messages, scheduler

logger = logging.getLogger(__name__)

_scan_locks: dict[str, asyncio.Lock] = {}
_inflight: dict[str, asyncio.Task] = {}
_dirty: set[str] = set()
_BLOCKABLE = {"queued", "running", "paused", "reviewing", "integration_ready"}
_NOTIFY_CHANGES = {"new", "escalated", "reopened"}


def _reset_for_tests() -> None:
    _scan_locks.clear()
    _inflight.clear()
    _dirty.clear()


async def _emit(user_ids: set[str], payload: dict) -> None:
    from cptr.socket.main import emit_to_user

    for user_id in user_ids:
        try:
            await emit_to_user(user_id, payload)
        except Exception:  # noqa: BLE001 - UI push must never break coordination
            logger.debug("[coord] emit failed", exc_info=True)


def _summary(row: CoordConflict, titles: dict[str, str], other_id: str | None) -> str:
    ev = row.evidence or {}
    files = ev.get("files") or []
    other = titles.get(other_id or "", other_id) if other_id else "the base branch"
    detail = {
        "file_overlap": "both tasks modify the same files",
        "shared_config": "both tasks modify shared configuration",
        "lockfile": "both tasks modify a dependency lockfile",
        "migration_heads": "both tasks add database migrations in the same directory",
        "interface": "a public interface changed and the other task uses it",
        "dependency_version": "the tasks require different versions of a dependency",
        "merge_conflict": "the branches do not merge cleanly",
        "base_divergence": "the branch no longer merges cleanly into its base",
        "dependency_failed": "a task this one depends on has failed or was ended",
    }.get(row.kind, row.kind.replace("_", " "))
    text = f"{row.level.upper()} conflict with {other}: {detail}"
    if files:
        text += f" ({', '.join(files[:6])}{'…' if len(files) > 6 else ''})"
    if row.kind == "interface":
        text += f"; symbols: {', '.join(sorted(ev.get('symbols', {}))[:6])}"
    return text + "."


def _blocked_task(row: CoordConflict, deps: set[tuple[str, str]]) -> str | None:
    """Which task to hold for a blocking conflict: the one that depends on the other."""
    if row.kind == "dependency_failed" or row.task_b is None:
        return row.task_a
    if (row.task_a, row.task_b) in deps:
        return row.task_a
    if (row.task_b, row.task_a) in deps:
        return row.task_b
    return None  # cycles and unrelated pairs: surfaced, nobody is paused


async def _block(task_id: str, row: CoordConflict) -> dict | None:
    task = await CoordTask.get_by_id(task_id)
    if not task or task.status not in _BLOCKABLE | {"blocked"}:
        return None
    blockers = list(task.blockers or [])
    if any(b.get("conflict_id") == row.id for b in blockers):
        return None
    blockers.append(
        {"kind": "conflict", "conflict_id": row.id, "detail": row.kind, "since": now_ms()}
    )
    fields: dict[str, Any] = {"blockers": blockers}
    if task.status != "blocked":
        fields["status"] = "blocked"
        fields["meta"] = {**(task.meta or {}), "status_before_block": task.status}
    await CoordTask.update_fields(task.id, **fields)
    return (await CoordTask.get_by_id(task.id)).to_dict()


async def _unblock(row: CoordConflict, active_chats: set[str]) -> list[dict]:
    changed = []
    for task_id in {row.task_a, row.task_b} - {None}:
        task = await CoordTask.get_by_id(task_id)
        if not task:
            continue
        blockers = [b for b in (task.blockers or []) if b.get("conflict_id") != row.id]
        if blockers == (task.blockers or []):
            continue
        fields: dict[str, Any] = {"blockers": blockers}
        if task.status == "blocked" and not blockers:
            meta = dict(task.meta or {})
            previous = meta.pop("status_before_block", "paused")
            if previous == "running" and task.chat_id not in active_chats:
                previous = "paused"
            fields["status"] = previous if previous in _BLOCKABLE else "paused"
            fields["meta"] = meta
        await CoordTask.update_fields(task.id, **fields)
        changed.append((await CoordTask.get_by_id(task.id)).to_dict())
    return changed


async def apply_effects(
    repo_key: str,
    upserted: list[tuple[CoordConflict, str]],
    resolved: list[CoordConflict],
) -> dict[str, list]:
    from cptr.utils.chat_task import get_active_chat_ids

    tasks = {t.id: t for t in await CoordTask.query(repo_key=repo_key)}
    titles = {tid: f'"{t.title}" ({tid})' for tid, t in tasks.items()}
    deps = set(await CoordTaskDep.all_for_repo(repo_key))
    active = get_active_chat_ids()
    changed_tasks: dict[str, dict] = {}
    notified: list[dict] = []

    for row, change in upserted:
        if change in _NOTIFY_CHANGES and CONFLICT_LEVELS.index(row.level) >= 1:
            for recipient in {row.task_a, row.task_b} - {None}:
                other = row.task_b if recipient == row.task_a else row.task_a
                kind = "incompatibility" if row.level in {"confirmed", "blocking"} else "conflict"
                if row.kind == "file_overlap":
                    kind = "file_overlap"
                await messages.post(
                    from_task=None,
                    repo_key=repo_key,
                    to_task=recipient,
                    kind=kind,
                    body=_summary(row, titles, other),
                    files=(row.evidence or {}).get("files"),
                    payload={"conflict_id": row.id, "level": row.level},
                )
            notified.append(row.to_dict())
        if row.level == "blocking" and row.status == "open":
            target = _blocked_task(row, deps)
            if target:
                updated = await _block(target, row)
                if updated:
                    changed_tasks[target] = updated
        elif row.level != "blocking" or row.status == "acknowledged":
            # De-escalated below blocking, or the user acknowledged it and chose to proceed.
            for t in await _unblock(row, active):
                changed_tasks[t["id"]] = t

    for row in resolved:
        for t in await _unblock(row, active):
            changed_tasks[t["id"]] = t

    # Persist what each task touches (survives restarts; used for message relevance).
    for task_id, task in tasks.items():
        cs = ch._cache.get(task_id)
        if cs is None:
            continue
        observed = sorted(cs.files)
        if (task.meta or {}).get("observed_files") != observed:
            current = await CoordTask.get_by_id(task_id)
            if current is None:  # deleted while this refresh ran
                continue
            await CoordTask.update_fields(
                task_id, meta={**(current.meta or {}), "observed_files": observed}
            )

    users = {t.user_id for t in tasks.values()}
    for row_dict in notified:
        await _emit(users, {"type": "coord:conflict", "conflict": row_dict})
    for row in resolved:
        await _emit(users, {"type": "coord:conflict", "conflict": row.to_dict()})
    for task_dict in changed_tasks.values():
        await _emit(users, {"type": "coord:task", "task": task_dict})
    return {"changed_tasks": list(changed_tasks), "notified": [r["id"] for r in notified]}


async def _identity_for_repo(repo_key: str):
    tasks = await CoordTask.query(repo_key=repo_key)
    if not tasks:
        return None
    try:
        from cptr.utils.identity import identity_for_user_id

        return await identity_for_user_id(tasks[0].user_id)
    except Exception:  # noqa: BLE001
        return None


async def refresh(repo_key: str, identity=None) -> dict:
    """Scan, apply effects and schedule one repository (serialised per repo)."""
    lock = _scan_locks.setdefault(repo_key, asyncio.Lock())
    async with lock:
        identity = identity or await _identity_for_repo(repo_key)
        upserted, resolved = await conflicts.scan(repo_key, identity)
        effects = await apply_effects(repo_key, upserted, resolved)
    started = await scheduler.schedule(repo_key)
    if started:
        tasks = await CoordTask.query(repo_key=repo_key)
        await _emit(
            {t.user_id for t in tasks},
            {"type": "coord:task", "task": None, "repo_key": repo_key, "started": started},
        )
    return {
        **effects,
        "started": started,
        "open_conflicts": len([r for r, _ in upserted if r.status == "open"]),
    }


def request_refresh(repo_key: str) -> None:
    """Fire-and-forget refresh, coalesced: a burst causes at most one extra scan."""
    task = _inflight.get(repo_key)
    if task and not task.done():
        _dirty.add(repo_key)
        return

    async def run():
        try:
            while True:
                _dirty.discard(repo_key)
                try:
                    await refresh(repo_key)
                except Exception:  # noqa: BLE001
                    logger.exception("[coord] refresh failed for %s", repo_key)
                if repo_key not in _dirty:
                    return
        finally:
            _inflight.pop(repo_key, None)

    _inflight[repo_key] = asyncio.create_task(run())


async def wait_idle() -> None:
    """Test helper: wait for in-flight refreshes."""
    while _inflight:
        await asyncio.gather(*list(_inflight.values()), return_exceptions=True)


# ── Agent turn hooks (called from chat_task) ────────────────


def task_brief(task: CoordTask) -> str:
    """The task's title, description and workspace, given to its agent on the first turn."""
    lines = ["", "", "[Coordinator] This chat is for the task below. Work only on this task.", ""]
    lines.append(f"Task: {task.title}")
    if task.description and task.description.strip():
        lines += ["Description:", task.description.strip()]
    if task.worktree_path:
        lines.append(
            f"Work in this worktree: {task.worktree_path} (branch {task.branch}). "
            "Commit your changes on that branch; do not switch branches or edit the main checkout."
        )
    else:
        lines.append("This is a read-only task: investigate and report, do not edit files.")
    return "\n".join(lines)


async def on_turn_start(chat_id: str, chat_meta: dict | None) -> str:
    """Mark the task running and return the coordinator block to append to the prompt."""
    task_id = (chat_meta or {}).get("coord_task_id")
    if not task_id:
        return ""
    task = await CoordTask.get_by_id(task_id)
    if not task or task.chat_id != chat_id:
        return ""
    blockers = task.blockers or []
    correcting = (
        task.status == "blocked"
        and blockers
        and all(b.get("kind") == "needs_correction" for b in blockers)
    )
    if correcting:
        # The agent is now working on the requested fix.
        meta = {**(task.meta or {}), "last_correction": blockers}
        meta.pop("status_before_block", None)
        await CoordTask.update_fields(
            task.id, expect_status="blocked", status="running", blockers=[], meta=meta
        )
        await _emit(
            {task.user_id},
            {"type": "coord:task", "task": (await CoordTask.get_by_id(task.id)).to_dict()},
        )
        task = await CoordTask.get_by_id(task.id)
    elif task.status in {"queued", "paused"}:
        meta = dict(task.meta or {})
        meta.pop("waiting_for_user", None)
        await CoordTask.update_fields(
            task.id, expect_status=task.status, status="running", meta=meta
        )
        task = await CoordTask.get_by_id(task.id)  # later writes must not restore old meta
        await _emit({task.user_id}, {"type": "coord:task", "task": task.to_dict()})
    block = ""
    if not (task.meta or {}).get("briefed_at"):
        # First turn in this task's chat: tell the agent what the task is, so
        # "start working on this" has something to refer to.
        block = task_brief(task)
        await CoordTask.update_fields(task.id, meta={**(task.meta or {}), "briefed_at": now_ms()})
    block += await messages.take_delivery(task.id)
    if task.status == "blocked":
        reasons = "; ".join(b.get("detail", b.get("kind", "")) for b in task.blockers or [])
        block += (
            "\n\n[Coordinator] This task is currently BLOCKED"
            + (f" ({reasons})" if reasons else "")
            + ". Avoid changes that deepen the conflict; resolve it or ask the user."
        )
    return block


# Seconds to wait after a turn ends before calling the task idle: an agent often
# resumes itself right away (e.g. when its background sub-agents report back).
TURN_END_GRACE_S = 3.0


async def _root_chat_id(chat_id: str) -> str:
    """The top-level chat a sub-agent chat belongs to (itself if it has no parent)."""
    from cptr.models.chats import Chat

    current = chat_id
    for _ in range(10):
        chat = await Chat.get_by_id(current)
        parent = ((chat.meta if chat else None) or {}).get("parent_chat_id")
        if not parent:
            return current
        current = parent
    return current


async def on_turn_end(chat_id: str, *, grace: float | None = None) -> str | None:
    """A turn ended in ``chat_id``. If its task's agent is now idle, mark it waiting.

    Idle means: no turn running in the task chat or any of its sub-agent chats and
    no pending timers. The task moves from ``running`` to ``queued`` with
    ``meta.waiting_for_user`` (shown as "Waiting for you"), which frees its slot
    so the scheduler can start the next queued task. The user's next message moves
    it back to running. Returns the task id when it was marked waiting.
    """
    from cptr.models.chats import Chat
    from cptr.utils.chat_task import get_active_chat_ids

    root = await _root_chat_id(chat_id)
    task = await CoordTask.get_by_chat(root)
    if not task or task.status != "running":
        return None
    await asyncio.sleep(TURN_END_GRACE_S if grace is None else grace)
    task = await CoordTask.get_by_id(task.id)
    if not task or task.status != "running":
        return None
    active = get_active_chat_ids()
    if root in active:
        return None
    descendants = await Chat.get_internal_descendants(root)
    if any(child.id in active for child in descendants):
        return None  # still working through its sub-agents
    if await Chat.get_pending_timers(root):
        return None  # it scheduled itself to continue later
    meta = {**(task.meta or {}), "waiting_for_user": now_ms()}
    if not await CoordTask.update_fields(
        task.id, expect_status="running", status="queued", meta=meta
    ):
        return None
    await _emit(
        {task.user_id},
        {"type": "coord:task", "task": (await CoordTask.get_by_id(task.id)).to_dict()},
    )
    request_refresh(task.repo_key)  # a slot is free: start the next queued task
    return task.id


class CoordinationEventSink:
    """Event-bus sink: refresh the task's repository when its agent turn ends."""

    async def handle_event(self, event) -> None:
        if event.event not in {"chat.finished", "chat.failed"}:
            return
        chat_id = (event.subject or {}).get("id")
        if not chat_id:
            return
        task = await CoordTask.get_by_chat(str(chat_id))
        if not task:
            return
        if event.event == "chat.failed":
            await CoordTask.update_fields(
                task.id, meta={**(task.meta or {}), "last_turn": {"failed": True, "at": now_ms()}}
            )
        request_refresh(task.repo_key)
