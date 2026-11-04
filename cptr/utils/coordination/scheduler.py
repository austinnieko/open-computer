"""Dependency graph and dependency-aware scheduling.

* Edges live in ``coord_task_deps``. Adding an edge that would close a cycle is
  rejected and recorded as a ``dependency_cycle`` conflict so it shows up in
  the dashboard.
* A queued task may start when every dependency is ``integration_ready`` or
  ``completed`` and a slot is free. Slots: ``coordination.max_writers``
  (default 2) for isolated code-writing tasks, ``coordination.max_readers``
  (default 2) for read-only tasks. A slot means "owns active work", not
  "is generating right now", so it limits how many agents edit at once.
* Starting a task means one turn in its bound chat with its start prompt, via
  the same ``start_task`` path automations use. Tasks without a start prompt
  (driven by the user typing) are never auto-started.
* Scheduling is triggered by events (registration, status change, turn end),
  never by a polling loop.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from cptr.models.coordination import CoordConflict, CoordTask, CoordTaskDep
from cptr.utils.config import now_ms
from cptr.utils.coordination import registry

logger = logging.getLogger(__name__)

READY = {"integration_ready", "completed"}
DEFAULT_MAX_WRITERS = 4
DEFAULT_MAX_READERS = 4

# Test hook: replaces the real "start a turn in this chat" call.
_launcher_override = None


def _set_launcher_for_tests(fn) -> None:
    global _launcher_override
    _launcher_override = fn


def find_cycle(edges: list[tuple[str, str]], new_edge: tuple[str, str]) -> list[str] | None:
    """Return the cycle ``[a, ..., a]`` that ``new_edge`` (task -> dependency) would close."""
    graph: dict[str, list[str]] = defaultdict(list)
    for task, dep in [*edges, new_edge]:
        graph[task].append(dep)
    start, target = new_edge
    stack = [(target, [start, target])]
    seen = set()
    while stack:
        node, path = stack.pop()
        if node == start:
            return path
        if node in seen:
            continue
        seen.add(node)
        for nxt in graph.get(node, []):
            stack.append((nxt, [*path, nxt]))
    return None


async def add_dependency(task_id: str, depends_on_id: str) -> dict:
    task = await CoordTask.get_by_id(task_id)
    dep = await CoordTask.get_by_id(depends_on_id)
    if not task or not dep:
        raise registry.CoordinationError("Unknown task")
    if task.repo_key != dep.repo_key:
        raise registry.CoordinationError("Dependencies must be in the same repository")
    if task_id == depends_on_id:
        raise registry.CoordinationError("A task cannot depend on itself")
    async with registry.repo_lock(task.repo_key):
        cycle = find_cycle(await CoordTaskDep.all_for_repo(task.repo_key), (task_id, depends_on_id))
        if cycle:
            await CoordConflict.upsert(
                repo_key=task.repo_key,
                task_a=task_id,
                task_b=depends_on_id,
                level="blocking",
                kind="dependency_cycle",
                evidence={"subject": "->".join(cycle), "cycle": cycle},
                fingerprint=f"cycle:{'->'.join(cycle)}",
            )
            raise registry.CoordinationError(
                "Dependency would create a cycle: " + " -> ".join(cycle)
            )
        await CoordTaskDep.add(task_id, depends_on_id)
    return {"task_id": task_id, "depends_on": depends_on_id}


async def remove_dependency(task_id: str, depends_on_id: str) -> bool:
    return await CoordTaskDep.remove(task_id, depends_on_id)


async def _limits() -> tuple[int, int]:
    from cptr.models import Config

    def as_int(value, default):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    return (
        as_int(await Config.get("coordination.max_writers"), DEFAULT_MAX_WRITERS),
        as_int(await Config.get("coordination.max_readers"), DEFAULT_MAX_READERS),
    )


DEFAULT_CHECKIN_MINUTES = 10
MAX_CHECKIN_MINUTES = 240


async def checkin_minutes() -> int:
    """Minutes before an agent that checked out *continuing* is nudged (0 = never)."""
    from cptr.models import Config

    value = await Config.get("coordination.checkin_minutes")
    if value is None:
        return DEFAULT_CHECKIN_MINUTES
    try:
        return min(MAX_CHECKIN_MINUTES, max(0, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_CHECKIN_MINUTES


def _waiting_blocker(waiting: list[str]) -> dict:
    return {"kind": "waiting_on", "detail": ", ".join(waiting), "tasks": waiting, "since": now_ms()}


async def schedule(repo_key: str) -> list[str]:
    """Start queued tasks whose dependencies are ready, within the slot limits.

    Returns the ids of tasks started.
    """
    max_writers, max_readers = await _limits()
    started: list[str] = []
    async with registry.repo_lock(repo_key):
        tasks = await CoordTask.query(repo_key=repo_key)
        by_id = {t.id: t for t in tasks}
        deps: dict[str, list[str]] = defaultdict(list)
        for task_id, dep in await CoordTaskDep.all_for_repo(repo_key):
            deps[task_id].append(dep)
        writers = sum(1 for t in tasks if t.status == "running" and t.worktree_path)
        readers = sum(1 for t in tasks if t.status == "running" and not t.worktree_path)

        for task in tasks:
            if task.status != "queued":
                continue
            waiting = [
                d for d in deps.get(task.id, []) if by_id.get(d) and by_id[d].status not in READY
            ]
            others = [b for b in (task.blockers or []) if b.get("kind") != "waiting_on"]
            blockers = others + ([_waiting_blocker(waiting)] if waiting else [])
            if blockers != (task.blockers or []):
                await CoordTask.update_fields(task.id, blockers=blockers)
            if waiting or others:
                continue
            start = (task.meta or {}).get("start")
            if not start or not task.chat_id:
                continue  # user-driven task: the user starts it by chatting
            is_writer = bool(task.worktree_path)
            if is_writer and writers >= max_writers or not is_writer and readers >= max_readers:
                continue
            if not await CoordTask.update_fields(task.id, expect_status="queued", status="running"):
                continue  # someone else moved it
            try:
                if deps.get(task.id):
                    from cptr.utils.coordination import lifecycle

                    # Bring in the finished dependencies' work before the agent starts.
                    await lifecycle.ensure_dependency_merges(task)
                await _launch(task, start)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[coord] failed to start %s: %s", task.id, exc)
                await CoordTask.update_fields(
                    task.id,
                    status="failed",
                    meta={**(task.meta or {}), "error": f"start failed: {exc}"},
                )
                continue
            # Re-read: the turn hook may have written meta (e.g. briefed_at) meanwhile.
            fresh = await CoordTask.get_by_id(task.id)
            meta = registry._milestone(fresh.meta if fresh else task.meta, "started")
            meta.pop("start", None)  # never re-launch the same prompt after a restart
            meta["start_prompt"] = start.get("prompt")
            await CoordTask.update_fields(task.id, meta=meta)
            started.append(task.id)
            if is_writer:
                writers += 1
            else:
                readers += 1
    return started


async def _launch(task: CoordTask, start: dict) -> None:
    if _launcher_override is not None:
        await _launcher_override(task, start)
        return
    await launch_turn(task.chat_id, task.user_id, start["prompt"], model_id=start.get("model_id"))


async def launch_turn(
    chat_id: str, user_id: str, prompt: str, *, model_id: str | None = None
) -> str:
    """Start one agent turn in ``chat_id`` with ``prompt``. Returns the assistant message id."""
    from cptr.models import Chat, ChatMessage
    from cptr.socket.main import emit_to_user
    from cptr.utils.chat_task import get_active_chat_ids, get_pending_input_lock, start_task
    from cptr.utils.model_targets import resolve_model_target

    chat = await Chat.get_by_id(chat_id)
    if not chat:
        raise registry.CoordinationError(f"Unknown chat {chat_id}")
    model_id = model_id or (chat.meta or {}).get("last_model")
    if not model_id:
        raise registry.CoordinationError("No model for this task's chat")
    async with get_pending_input_lock(chat_id):
        if chat_id in get_active_chat_ids():
            raise registry.CoordinationError("Chat is already running a turn")
        parent = None
        msgs = await ChatMessage.get_all_by_chat(chat_id)
        done = [m for m in msgs if m.done]
        if done:
            parent = done[-1].id
        user_msg = await ChatMessage.create(
            chat_id=chat_id,
            role="user",
            content=prompt,
            parent_id=parent,
            meta={"coordination": True},
            created_at=now_ms(),
        )
        assistant = await ChatMessage.create(
            chat_id=chat_id,
            role="assistant",
            content="",
            parent_id=user_msg.id,
            model=model_id,
            done=False,
            created_at=now_ms(),
        )
        await Chat.update_current_message(chat_id, assistant.id, now_ms())
    target = await resolve_model_target(model_id)
    start_task(
        None,
        message_id=assistant.id,
        chat_id=chat_id,
        user_id=user_id,
        workspace=(chat.meta or {}).get("workspace") or "",
        target=target,
    )
    await emit_to_user(
        user_id, {"chat_id": chat_id, "message_id": assistant.id, "pending_inputs_processed": True}
    )
    return assistant.id
