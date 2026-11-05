"""Coordination API: tasks, worktree bindings and their live state."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cptr.models.chats import Chat
from cptr.models.coordination import (
    CoordClaim,
    CoordConflict,
    CoordIntegration,
    CoordMessage,
    CoordTask,
    CoordTaskDep,
)
from cptr.routers.git import _require_gh_mutation_allowed, _root_identity
from cptr.utils import gh
from cptr.utils.coordination import (
    coordinator,
    integration,
    messages,
    prs,
    registry,
    scheduler,
    validation,
)
from cptr.utils.coordination import worktrees as wt
from cptr.utils.chat_export import chat_directory
from cptr.utils.config import now_ms
from cptr.utils.identity import ExecutionIdentity
from cptr.utils.runtime import FileError, Runtime
from cptr.socket.main import emit_to_user

router = APIRouter(prefix="/api/coordination", tags=["coordination"])


def _user_id(request: Request) -> str:
    auth = getattr(request.state, "auth", None)
    if not auth or not getattr(auth, "user_id", None):
        raise HTTPException(401, "authentication required")
    return auth.user_id


async def _own_task(request: Request, task_id: str) -> CoordTask:
    task = await CoordTask.get_by_id(task_id)
    if not task or task.user_id != _user_id(request):
        raise HTTPException(404, "task not found")
    return task


async def _identity(request: Request, root: str | None) -> tuple[str, ExecutionIdentity]:
    return await _root_identity(request, root)


async def _emit(user_id: str, task: dict) -> None:
    await emit_to_user(user_id, {"type": "coord:task", "task": task})


class RegisterTaskRequest(BaseModel):
    root: str
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    branch: str | None = None
    base_ref: str | None = None
    chat_id: str | None = None
    agent: str | None = None
    depends_on: list[str] = []
    files: list[str] = []
    isolate: bool = True
    start_prompt: str | None = None
    model_id: str | None = None
    test_command: str | None = None
    targeted_test_command: str | None = None
    create_chat: bool = False
    # False keeps a task with instructions as a Draft until the user clicks Start.
    start_now: bool = True


class StartTaskRequest(BaseModel):
    prompt: str | None = None
    model_id: str | None = None


DEFAULT_START_PROMPT = "Start working on this task."


class BindChatRequest(BaseModel):
    chat_id: str


class StatusRequest(BaseModel):
    status: str
    reason: str | None = None
    expect: str | None = None


class RemoveWorktreeRequest(BaseModel):
    confirm: bool = False


class DeleteTaskRequest(BaseModel):
    confirm: bool = False
    remove_worktree: bool = False


class DependencyRequest(BaseModel):
    depends_on: str


class MessageRequest(BaseModel):
    kind: str = "note"
    body: str = Field(min_length=1, max_length=2000)
    files: list[str] = []


class ConflictStatusRequest(BaseModel):
    status: str  # acknowledged | resolved | open


# Task chats whose sidebar file is known to exist (skips re-checking each poll).
_chat_files_ok: set[str] = set()


async def _ensure_chat_file(request: Request, workspace: str, chat_id: str) -> None:
    """Give a task chat the empty chat file a normal new chat gets.

    The sidebar lists a workspace's chats from these files, so without one an
    open task chat shows as a bare row with no time or menu, and can't be
    deleted from the sidebar. Never overwrites an existing file.
    """
    if chat_id in _chat_files_ok:
        return
    path = str(chat_directory(workspace) / f"{chat_id}.json")
    try:
        try:
            await Runtime.stat(request, path)
        except FileError as exc:
            if exc.status_code != 404:
                return
            await Runtime.write_file(request, path, "{}")
        _chat_files_ok.add(chat_id)
    except Exception:  # never fail a task request over the sidebar file
        pass


async def _origin_for_legacy(
    task: CoordTask, repo: wt.RepoIdentity, branch_paths: dict[str, str]
) -> str:
    """Best guess at the folder an older task was started from: the worktree that
    has its base branch checked out, else the repository's main checkout."""
    base = (task.meta or {}).get("base_branch") or task.base_ref or ""
    return branch_paths.get(base, repo.main_root)


async def _file_chat_under(request: Request, chat_id: str, origin: str) -> None:
    """Make the task chat belong to ``origin`` (the folder the task started from):
    its workspace and its sidebar file. The old file is removed only after the new
    one exists; nothing happens if the chat is already there."""
    chat = await Chat.get_by_id(chat_id)
    if not chat:
        return
    current = (chat.meta or {}).get("workspace")
    if current and os.path.realpath(current) == origin:
        await _ensure_chat_file(request, origin, chat_id)
        return
    await Chat.update_meta(
        chat_id, {**(chat.meta or {}), "workspace": origin}, updated_at=chat.updated_at or 0
    )
    _chat_files_ok.discard(chat_id)
    # Write the chat's file in its new folder first (rebuilt from the database),
    # then drop the old copy, which would otherwise list it in both folders.
    from cptr.utils.chat_export import export_chat_to_file

    await export_chat_to_file(request, chat_id)
    if current:
        old = str(chat_directory(current) / f"{chat_id}.json")
        try:
            await Runtime.stat(request, str(chat_directory(origin) / f"{chat_id}.json"))
            await Runtime.delete_item(request, old)
        except Exception:  # noqa: BLE001 - never fail the task list over a file
            pass
    await _ensure_chat_file(request, origin, chat_id)


class LimitsRequest(BaseModel):
    max_writers: int | None = Field(default=None, ge=1, le=16)
    # Minutes before an agent waiting on its own background work is nudged; 0 = never.
    checkin_minutes: int | None = Field(default=None, ge=0, le=scheduler.MAX_CHECKIN_MINUTES)


async def _limits_dict() -> dict:
    max_writers, max_readers = await scheduler._limits()
    return {
        "max_writers": max_writers,
        "max_readers": max_readers,
        "checkin_minutes": await scheduler.checkin_minutes(),
    }


@router.get("/limits")
async def get_limits(request: Request):
    _user_id(request)
    return await _limits_dict()


@router.put("/limits")
async def set_limits(request: Request, body: LimitsRequest):
    """Instance-wide agent settings (admin only): how many task agents may write
    code at once, and how often an agent waiting on background work is checked on."""
    from cptr.models import Config
    from cptr.routers.admin import require_admin

    require_admin(request)
    values = {}
    if body.max_writers is not None:
        values["coordination.max_writers"] = body.max_writers
    if body.checkin_minutes is not None:
        values["coordination.checkin_minutes"] = body.checkin_minutes
    if values:
        await Config.upsert(values)
    # A higher limit can start queued tasks right away.
    for repo_key in {t.repo_key for t in await CoordTask.query()}:
        coordinator.request_refresh(repo_key)
    return await _limits_dict()


@router.get("/tasks")
async def list_tasks(request: Request, root: str):
    user_id = _user_id(request)
    root, identity = await _identity(request, root)
    try:
        repo = await wt.repo_identity(root, identity)
    except wt.WorktreeError as exc:
        raise HTTPException(400, str(exc)) from exc
    from cptr.utils.chat_task import get_active_chat_ids

    tasks = await CoordTask.query(user_id=user_id, repo_key=repo.key)
    # Which folder each task was started from (older tasks: inferred once).
    legacy = [t for t in tasks if not (t.meta or {}).get("origin_root")]
    if legacy:
        try:
            branch_paths = {
                (w.get("branch") or "").removeprefix("refs/heads/"): os.path.realpath(w["path"])
                for w in await wt.list_worktrees(repo.main_root, identity)
                if w.get("branch") and w.get("path")
            }
        except Exception:  # noqa: BLE001
            branch_paths = {}
        for t in legacy:
            origin = await _origin_for_legacy(t, repo, branch_paths)
            await CoordTask.update_fields(t.id, meta={**(t.meta or {}), "origin_root": origin})
        tasks = await CoordTask.query(user_id=user_id, repo_key=repo.key)
    deps = await CoordTaskDep.all_for_repo(repo.key)
    active = get_active_chat_ids()
    chat_ids = [t.chat_id for t in tasks if t.chat_id]
    existing = {c.id for c in await Chat.get_by_ids(chat_ids)} if chat_ids else set()
    # Each task chat lives in the sidebar under the folder its task started from.
    for t in tasks:
        if t.chat_id in existing and t.chat_id not in _chat_files_ok:
            await _file_chat_under(request, t.chat_id, t.meta["origin_root"])

    def _row(t: CoordTask) -> dict:
        row = t.to_dict()
        # The chat was deleted (before deleting a chat removed its task too).
        row["chat_missing"] = bool(t.chat_id) and t.chat_id not in existing
        return row

    return {
        "repo_root": repo.main_root,
        "root": os.path.realpath(root),  # compare with each task's meta.origin_root
        "repo_key": repo.key,
        "active_chats": sorted(t.chat_id for t in tasks if t.chat_id and t.chat_id in active),
        "tasks": [_row(t) for t in tasks],
        # Slot limits, so the dashboard can say why a queued task hasn't started.
        "limits": await _limits_dict(),
        "dependencies": [{"task_id": a, "depends_on": b} for a, b in deps],
    }


# Finished tasks drop off Home's summary after this long.
SUMMARY_RECENT_MS = 24 * 60 * 60 * 1000


@router.get("/summary")
async def summary(request: Request, usage: bool = False):
    """Every task agent of this user across all repositories (the Agents
    dashboard): tasks still in progress, plus ones that finished in the last
    day. ``usage=true`` adds per-task usage and open conflicts per repository."""
    from cptr.utils.chat_task import get_active_chat_ids
    from cptr.utils.config import now_ms

    user_id = _user_id(request)
    cutoff = now_ms() - SUMMARY_RECENT_MS
    tasks = [
        t
        for t in await CoordTask.query(user_id=user_id)
        if t.status not in {"completed", "ended"} or (t.updated_at or 0) >= cutoff
    ]
    chat_ids = [t.chat_id for t in tasks if t.chat_id]
    existing = {c.id for c in await Chat.get_by_ids(chat_ids)} if chat_ids else set()
    active = get_active_chat_ids()
    rows = []
    for t in tasks:
        row = t.to_dict()
        row["chat_missing"] = bool(t.chat_id) and t.chat_id not in existing
        rows.append(row)
    deps = []
    for key in {t.repo_key for t in tasks}:
        deps += [{"task_id": a, "depends_on": b} for a, b in await CoordTaskDep.all_for_repo(key)]
    result = {
        "tasks": rows,
        "active_chats": sorted(c for c in chat_ids if c in active),
        "limits": await _limits_dict(),
        "dependencies": deps,
    }
    if usage:
        totals = dict.fromkeys(USAGE_KEYS, 0)
        per_task = {}
        index = await _subagent_index()
        chats = {c.id: c for c in await Chat.get_by_ids(chat_ids)}
        for t in tasks:
            per_task[t.id] = await _task_usage(t, index, chats)
            _add_usage(totals, per_task[t.id])
        result["usage"] = {"tasks": per_task, "totals": totals}
        # Open file conflicts per repository, for the dashboard's attention count.
        result["conflicts"] = {
            key: len(await CoordConflict.query(key, statuses={"open", "acknowledged"}))
            for key in {t.repo_key for t in tasks}
        }
    return result


@router.get("/tasks/{task_id}/diff")
async def task_diff(request: Request, task_id: str):
    """Everything the task changed relative to its base: commits plus uncommitted edits."""
    from cptr.utils.coordination import changes as ch
    from cptr.utils.git import _parse_diff, _run

    task = await _own_task(request, task_id)
    if not task.worktree_path:
        raise HTTPException(400, "task has no worktree")
    _, identity = await _identity(request, task.repo_root)
    cs = await ch.compute(task, identity)
    if cs is None:
        raise HTTPException(409, "task worktree is missing")
    _, out, _ = await _run(
        "diff",
        "--unified=3",
        "--color=never",
        cs.merge_base,
        cwd=task.worktree_path,
        check=False,
        identity=identity,
    )
    result = _parse_diff(out)
    result["untracked"] = sorted(
        p
        for p, s in cs.uncommitted.items()
        if s == "A" and p not in {f.get("path") for f in result.get("files", [])}
    )
    result["merge_base"] = cs.merge_base
    result["head"] = cs.head
    return result


async def _stop_turns(task: CoordTask) -> int:
    """Cancel the task chat's running agent turns. Returns how many were stopped."""
    from cptr.utils.chat_task import _task_chat, cancel_task, is_running

    stopped = 0
    if task.chat_id:
        for message_id, chat_id in list(_task_chat.items()):
            if chat_id == task.chat_id and is_running(message_id):
                stopped += int(await cancel_task(message_id))
    return stopped


async def forget_task_of_deleted_chat(chat_id: str, user_id: str) -> str | None:
    """Remove the task whose agent chat is being deleted. Returns its id.

    Called by chat deletion: the user deleted the task's chat, so the task goes
    too. Its branch, commits and worktree folder are always kept.
    """
    task = await CoordTask.get_by_chat(chat_id)
    if not task or task.user_id != user_id:
        return None
    await _stop_turns(task)
    await CoordTask.delete_with_records(task.id)
    await emit_to_user(user_id, {"type": "coord:task_deleted", "task_id": task.id})
    coordinator.request_refresh(task.repo_key)
    return task.id


@router.post("/tasks/{task_id}/pause")
async def pause_task(request: Request, task_id: str):
    """Stop the task's running agent turn (if any) and mark it paused."""
    task = await _own_task(request, task_id)
    stopped = await _stop_turns(task)
    if task.status != "paused":
        try:
            await registry.set_status(task.id, "paused", reason="paused by user")
        except registry.CoordinationError as exc:
            raise HTTPException(409, str(exc)) from exc
    result = (await CoordTask.get_by_id(task.id)).to_dict()
    await _emit(task.user_id, result)
    coordinator.request_refresh(task.repo_key)
    return {"task": result, "stopped_turns": stopped}


@router.post("/tasks/{task_id}/end")
async def end_task(request: Request, task_id: str):
    """Stop the task's agent and mark it ended. Branch, worktree and chat are kept;
    tasks that depend on it become blocked until the dependency is removed."""
    task = await _own_task(request, task_id)
    if task.status == "completed":
        raise HTTPException(409, "task is already completed")
    stopped = await _stop_turns(task)
    if task.status != "ended":
        try:
            await registry.set_status(task.id, "ended", reason="ended by user")
        except registry.CoordinationError as exc:
            raise HTTPException(409, str(exc)) from exc
    result = (await CoordTask.get_by_id(task.id)).to_dict()
    await _emit(task.user_id, result)
    coordinator.request_refresh(task.repo_key)
    return {"task": result, "stopped_turns": stopped}


@router.post("/tasks/{task_id}/done")
async def mark_task_done(request: Request, task_id: str):
    """Mark the task done by hand (its work is finished or landed elsewhere), so
    tasks that depend on it start. Also turns an ended task into a done one."""
    task = await _own_task(request, task_id)
    if task.status == "running":
        await _stop_turns(task)
        try:
            await registry.set_status(task.id, "paused", reason="stopped to mark done")
        except registry.CoordinationError:
            pass
    try:
        result = await registry.mark_done(task.id)
    except registry.CoordinationError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _emit(task.user_id, result)
    coordinator.request_refresh(task.repo_key)
    return {"task": result}


@router.delete("/tasks/{task_id}")
async def delete_task(request: Request, task_id: str, body: DeleteTaskRequest):
    """Delete a finished task's record. Requires ``confirm: true`` from the user.

    The branch and its commits are always kept. The worktree folder is removed
    only with ``remove_worktree: true`` (refused if it has uncommitted changes).
    The chat stays as an ordinary chat.
    """
    task = await _own_task(request, task_id)
    if not body.confirm:
        raise HTTPException(409, "Deleting a task requires explicit user approval")
    chat = await Chat.get_by_id(task.chat_id) if task.chat_id else None
    chat_missing = bool(task.chat_id) and chat is None
    # With its chat gone no agent can run it, so it can be deleted as it is.
    if task.status not in {"ended", "completed", "failed"} and not chat_missing:
        raise HTTPException(409, f"task is {task.status}; end it first")
    removed_worktree = False
    if body.remove_worktree and task.worktree_path:
        _, identity = await _identity(request, task.repo_root)
        try:
            await wt.remove_worktree(
                wt.RepoIdentity(task.repo_key, task.repo_root),
                task.worktree_path,
                user_confirmed=True,
                identity=identity,
            )
        except wt.WorktreeError as exc:
            raise HTTPException(409, str(exc)) from exc
        removed_worktree = True
    if chat:
        if (chat.meta or {}).get("coord_task_id") == task.id:
            meta = dict(chat.meta or {})
            meta.pop("coord_task_id", None)
            if removed_worktree or meta.get("cwd") == task.worktree_path:
                meta.pop("cwd", None)  # back to the workspace folder
            await Chat.update_meta(chat.id, meta, now_ms())
    await CoordTask.delete_with_records(task.id)
    await emit_to_user(task.user_id, {"type": "coord:task_deleted", "task_id": task.id})
    coordinator.request_refresh(task.repo_key)
    return {"ok": True, "branch_kept": task.branch, "worktree_removed": removed_worktree}


@router.post("/tasks/{task_id}/start")
async def start_task_route(request: Request, task_id: str, body: StartTaskRequest):
    """Start a Draft task: it gets in line and runs when a slot is free.

    Uses ``body.prompt``, else the instructions saved with the task, else a plain
    "start working" prompt. Typing in the task's chat starts it too.
    """
    task = await _own_task(request, task_id)
    if not task.chat_id:
        raise HTTPException(409, "task has no agent chat")
    meta = dict(task.meta or {})
    if task.status != "queued" or meta.get("start"):
        raise HTTPException(409, f"task is {task.status}; only a draft can be started")
    draft = meta.get("draft_start") or {}
    chat = await Chat.get_by_id(task.chat_id)
    model_id = (
        body.model_id
        or draft.get("model_id")
        or ((chat.meta or {}) if chat else {}).get("last_model")
    )
    if not model_id:
        raise HTTPException(409, "Choose a model for this task's chat first")
    prompt = (body.prompt or draft.get("prompt") or DEFAULT_START_PROMPT).strip()
    meta.pop("draft_start", None)
    meta.pop("waiting_for_user", None)
    meta["start"] = {"prompt": prompt, "model_id": model_id}
    await CoordTask.update_fields(task.id, expect_status="queued", meta=meta)
    coordinator.request_refresh(task.repo_key)
    result = (await CoordTask.get_by_id(task.id)).to_dict()
    await _emit(task.user_id, result)
    return result


@router.post("/tasks/{task_id}/resume")
async def resume_task(request: Request, task_id: str):
    """Queue the task again. With an agent chat, the scheduler continues it when a
    slot is free and its dependencies are ready (one turn, "continue")."""
    task = await _own_task(request, task_id)
    if task.status not in {"paused", "failed"}:
        raise HTTPException(409, f"task is {task.status}")
    try:
        await registry.set_status(task.id, "queued", reason="resumed by user")
    except registry.CoordinationError as exc:
        raise HTTPException(409, str(exc)) from exc
    current = await CoordTask.get_by_id(task.id)
    if current.chat_id:
        meta = dict(current.meta or {})
        prompt = (
            "Continue this task from where you left off. Check `git status` and "
            "`git log` in this worktree first; do not redo work that is already committed."
        )
        note = ((meta.get("checkout") or {}).get("note") or "").strip()
        if note:
            prompt += f"\n\nThe last check-out on this task said: {note}"
        meta["start"] = {"prompt": prompt, "model_id": None}
        await CoordTask.update_fields(task.id, meta=meta)
    coordinator.request_refresh(task.repo_key)
    result = (await CoordTask.get_by_id(task.id)).to_dict()
    await _emit(task.user_id, result)
    return result


@router.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str):
    task = await _own_task(request, task_id)
    _, identity = await _identity(request, task.repo_root)
    state = await wt.worktree_state(task.worktree_path, identity) if task.worktree_path else None
    claims = await CoordClaim.for_task(task.id)
    return {
        "task": task.to_dict(),
        "worktree": state,
        "claims": [{"kind": c.kind, "resource": c.resource} for c in claims],
    }


@router.post("/tasks")
async def register(request: Request, body: RegisterTaskRequest):
    user_id = _user_id(request)
    root, identity = await _identity(request, body.root)
    origin = os.path.realpath(root)
    if body.chat_id:
        chat = await Chat.get_by_id(body.chat_id)
        if not chat or chat.user_id != user_id:
            raise HTTPException(404, "chat not found")
    elif body.create_chat:
        try:
            await wt.repo_identity(root, identity)  # must be a Git repository
        except wt.WorktreeError as exc:
            raise HTTPException(400, str(exc)) from exc
        # The chat belongs to the folder the task was started from, so a task
        # opened from a worktree is listed under that worktree in the sidebar.
        meta = {"workspace": origin}
        if body.model_id:
            meta["last_model"] = body.model_id
        chat = await Chat.create(user_id, body.title.strip()[:120], meta=meta, created_at=now_ms())
        body.chat_id = chat.id
        await _ensure_chat_file(request, origin, chat.id)
    try:
        result = await registry.register_task(
            user_id=user_id,
            repo_path=root,
            title=body.title,
            description=body.description,
            branch=body.branch,
            base_ref=body.base_ref,
            chat_id=body.chat_id,
            agent=body.agent,
            depends_on=body.depends_on,
            files=body.files,
            isolate=body.isolate,
            start_prompt=body.start_prompt,
            model_id=body.model_id,
            test_command=body.test_command,
            targeted_test_command=body.targeted_test_command,
            identity=identity,
        )
    except (registry.CoordinationError, wt.WorktreeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if (result["task"].get("meta") or {}).get("origin_root") is None and not result.get("reused"):
        meta = {**(result["task"].get("meta") or {}), "origin_root": origin}
        await CoordTask.update_fields(result["task"]["id"], meta=meta)
        result["task"] = (await CoordTask.get_by_id(result["task"]["id"])).to_dict()
    if not body.start_now and (result["task"].get("meta") or {}).get("start"):
        # Keep it a Draft: save the instructions for when the user clicks Start.
        meta = dict(result["task"]["meta"])
        meta["draft_start"] = meta.pop("start")
        await CoordTask.update_fields(result["task"]["id"], meta=meta)
        result["task"] = (await CoordTask.get_by_id(result["task"]["id"])).to_dict()
    await _emit(user_id, result["task"])
    coordinator.request_refresh(result["task"]["repo_key"])
    return result


@router.post("/tasks/{task_id}/bind")
async def bind(request: Request, task_id: str, body: BindChatRequest):
    task = await _own_task(request, task_id)
    chat = await Chat.get_by_id(body.chat_id)
    if not chat or chat.user_id != task.user_id:
        raise HTTPException(404, "chat not found")
    try:
        result = await registry.bind_chat(task.id, body.chat_id)
    except registry.CoordinationError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _emit(task.user_id, result)
    return result


@router.post("/tasks/{task_id}/status")
async def set_status(request: Request, task_id: str, body: StatusRequest):
    task = await _own_task(request, task_id)
    try:
        result = await registry.set_status(
            task.id, body.status, reason=body.reason, expect=body.expect
        )
    except registry.CoordinationError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _emit(task.user_id, result)
    coordinator.request_refresh(task.repo_key)
    return result


@router.post("/tasks/{task_id}/worktree/remove")
async def remove_worktree(request: Request, task_id: str, body: RemoveWorktreeRequest):
    """Remove a task's worktree (branch kept). Requires ``confirm: true`` from the user."""
    task = await _own_task(request, task_id)
    if not task.worktree_path:
        raise HTTPException(400, "task has no worktree")
    if task.status not in {"completed", "failed", "ended", "paused", "blocked"}:
        raise HTTPException(409, f"task is {task.status}; pause or finish it first")
    _, identity = await _identity(request, task.repo_root)
    try:
        await wt.remove_worktree(
            wt.RepoIdentity(task.repo_key, task.repo_root),
            task.worktree_path,
            user_confirmed=body.confirm,
            identity=identity,
        )
    except wt.WorktreeError as exc:
        raise HTTPException(409, str(exc)) from exc
    await CoordClaim.release(task.id, kinds={"worktree"})
    return {"ok": True, "branch_kept": task.branch}


async def _repo_key(request: Request, root: str) -> str:
    root, identity = await _identity(request, root)
    try:
        return (await wt.repo_identity(root, identity)).key
    except wt.WorktreeError as exc:
        raise HTTPException(400, str(exc)) from exc


async def _owned_repo(request: Request, repo_key: str, *, required: bool = True) -> bool:
    user_id = _user_id(request)
    if await CoordTask.query(user_id=user_id, repo_key=repo_key):
        return True
    if required:
        raise HTTPException(404, "no tasks for this repository")
    return False


@router.post("/tasks/{task_id}/dependencies")
async def add_dependency(request: Request, task_id: str, body: DependencyRequest):
    task = await _own_task(request, task_id)
    await _own_task(request, body.depends_on)
    try:
        result = await scheduler.add_dependency(task.id, body.depends_on)
    except registry.CoordinationError as exc:
        raise HTTPException(409, str(exc)) from exc
    coordinator.request_refresh(task.repo_key)
    return result


@router.delete("/tasks/{task_id}/dependencies/{depends_on}")
async def remove_dependency(request: Request, task_id: str, depends_on: str):
    task = await _own_task(request, task_id)
    removed = await scheduler.remove_dependency(task.id, depends_on)
    coordinator.request_refresh(task.repo_key)
    return {"removed": removed}


USAGE_KEYS = ("tokens", "cost_usd", "duration_ms", "tool_calls", "turns", "files")


# Usage of one chat, by (chat id, updated_at): the chat is touched when a turn
# ends, so a finished chat's numbers never change until its next turn.
_chat_usage_cache: dict[tuple[str, int], dict] = {}
_CHAT_USAGE_CACHE_MAX = 5000


async def _subagent_index() -> dict[str, list[Chat]]:
    """parent chat id -> its internal sub-agent chats (all levels), built once
    per request instead of scanning every chat once per task."""
    from cptr.models.chats import is_internal_chat
    from cptr.utils.db import get_db
    from sqlalchemy import select

    async with await get_db() as db:
        result = await db.execute(select(Chat).where(Chat.meta.is_not(None)))
        candidates = [c for c in result.scalars().all() if is_internal_chat(c.meta)]
    children: dict[str, list[Chat]] = {}
    for chat in candidates:
        parent = (chat.meta or {}).get("parent_chat_id")
        if parent:
            children.setdefault(parent, []).append(chat)
    return children


def _descendants(index: dict[str, list[Chat]], root: str) -> list[Chat]:
    out: list[Chat] = []
    stack = [root]
    seen = {root}
    while stack:
        for child in index.get(stack.pop(), []):
            if child.id not in seen:
                seen.add(child.id)
                out.append(child)
                stack.append(child.id)
    return out


async def _chat_usage(chat_id: str, updated_at: int | None, active: set[str]) -> dict:
    from cptr.models.chats import ChatMessage
    from cptr.routers.chat import _tally_messages

    key = (chat_id, int(updated_at or 0))
    if chat_id not in active and key in _chat_usage_cache:
        return _chat_usage_cache[key]
    stats = _tally_messages(await ChatMessage.get_all_by_chat(chat_id))
    usage = {
        "tokens": stats["totals"]["total"],
        "cost_usd": stats["totals"]["cost_usd"],
        "duration_ms": stats["totals"]["duration_ms"],
        "tool_calls": sum(t["count"] for t in stats["tools"]),
        "turns": len(stats["turns"]),
    }
    if chat_id not in active:  # a running turn's numbers are still changing
        if len(_chat_usage_cache) >= _CHAT_USAGE_CACHE_MAX:
            _chat_usage_cache.clear()
        _chat_usage_cache[key] = usage
    return usage


async def _task_usage(
    task: CoordTask,
    index: dict[str, list[Chat]] | None = None,
    chats: dict[str, Chat] | None = None,
) -> dict:
    """Tokens, cost, working time, tool calls, turns and files of one task, from
    its chat and its sub-agent chats (cached per chat until it changes)."""
    from cptr.utils.chat_task import get_active_chat_ids

    usage = dict.fromkeys(USAGE_KEYS, 0)
    # Declared files plus the ones Git shows changed in its worktree.
    usage["files"] = len(set(task.files or []) | set((task.meta or {}).get("observed_files") or []))
    if task.chat_id:
        if index is None:
            index = await _subagent_index()
        root = (chats or {}).get(task.chat_id) or await Chat.get_by_id(task.chat_id)
        members = ([root] if root else []) + [
            c for c in _descendants(index, task.chat_id) if (c.meta or {}).get("type") == "subagent"
        ]
        active = get_active_chat_ids()
        for chat in members:
            one = await _chat_usage(chat.id, chat.updated_at, active)
            usage["tokens"] += one["tokens"]
            usage["cost_usd"] += one["cost_usd"]
            usage["duration_ms"] += one["duration_ms"]
            usage["tool_calls"] += one["tool_calls"]
            if chat.id == task.chat_id:
                usage["turns"] = one["turns"]
    usage["cost_usd"] = round(usage["cost_usd"], 6)
    return usage


def _add_usage(totals: dict, usage: dict) -> None:
    for key in USAGE_KEYS:
        totals[key] += usage[key]
    totals["cost_usd"] = round(totals["cost_usd"], 6)


@router.get("/usage")
async def list_usage(request: Request, root: str):
    """Per-task agent usage (tokens, cost, working time, tool calls, turns, files)
    plus repository totals, from each task chat and its sub-agent chats."""
    repo_key = await _repo_key(request, root)
    tasks = await CoordTask.query(user_id=_user_id(request), repo_key=repo_key)
    totals = dict.fromkeys(USAGE_KEYS, 0)
    per_task: dict[str, dict] = {}
    index = await _subagent_index()
    chats = {c.id: c for c in await Chat.get_by_ids([t.chat_id for t in tasks if t.chat_id])}
    for task in tasks:
        per_task[task.id] = await _task_usage(task, index, chats)
        _add_usage(totals, per_task[task.id])
    return {"tasks": per_task, "totals": totals}


@router.get("/conflicts")
async def list_conflicts(request: Request, root: str, include_resolved: bool = False):
    repo_key = await _repo_key(request, root)
    if not await _owned_repo(request, repo_key, required=False):
        return {"conflicts": []}
    statuses = None if include_resolved else {"open", "acknowledged"}
    return {
        "conflicts": [c.to_dict() for c in await CoordConflict.query(repo_key, statuses=statuses)]
    }


@router.post("/conflicts/{conflict_id}/status")
async def set_conflict_status(request: Request, conflict_id: str, body: ConflictStatusRequest):
    if body.status not in {"acknowledged", "resolved", "open"}:
        raise HTTPException(400, "status must be acknowledged, resolved or open")
    rows = [r for r in await CoordConflict.query_by_id(conflict_id)]
    if not rows:
        raise HTTPException(404, "conflict not found")
    await _owned_repo(request, rows[0].repo_key)
    await CoordConflict.set_status(conflict_id, body.status)
    # Acknowledging a blocking conflict releases the task it held; a user-resolved
    # conflict reopens on the next scan if the evidence is still there.
    coordinator.request_refresh(rows[0].repo_key)
    return {"ok": True}


@router.get("/messages")
async def list_messages(request: Request, root: str, task_id: str | None = None):
    repo_key = await _repo_key(request, root)
    if not await _owned_repo(request, repo_key, required=False):
        return {"messages": []}
    rows = await CoordMessage.for_repo(repo_key, task_id=task_id)
    return {"messages": [m.to_dict() for m in rows]}


@router.post("/tasks/{task_id}/messages")
async def send_message(request: Request, task_id: str, body: MessageRequest):
    """A user note to one task's agent, delivered at the start of its next turn."""
    task = await _own_task(request, task_id)
    try:
        rows = await messages.post(
            from_task=None,
            repo_key=task.repo_key,
            to_task=task.id,
            kind=body.kind,
            body=body.body,
            files=body.files,
        )
    except messages.MessageError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"messages": rows}


@router.post("/scan")
async def scan(request: Request, root: str):
    """Run conflict detection and scheduling now (normally event-driven)."""
    repo_key = await _repo_key(request, root)
    await _owned_repo(request, repo_key)
    return await coordinator.refresh(repo_key)


class IntegrationRequest(BaseModel):
    root: str
    task_ids: list[str] | None = None
    run_tests: bool = True


class PublishRequest(BaseModel):
    confirm: bool = False
    draft: bool = False


class CorrectionRequest(BaseModel):
    note: str | None = None


@router.post("/tasks/{task_id}/validate")
async def validate(request: Request, task_id: str):
    task = await _own_task(request, task_id)
    _, identity = await _identity(request, task.repo_root)
    try:
        result = await validation.validate_task(task.id, identity)
    except registry.CoordinationError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _emit(task.user_id, (await CoordTask.get_by_id(task.id)).to_dict())
    return result


@router.get("/tasks/{task_id}/readiness")
async def readiness(request: Request, task_id: str):
    task = await _own_task(request, task_id)
    _, identity = await _identity(request, task.repo_root)
    reasons = await validation.readiness(task, {task.id}, identity)
    return {"ready": not reasons, "reasons": reasons}


@router.post("/integrations")
async def request_integration(request: Request, body: IntegrationRequest):
    """Start an integration run in a separate worktree. Never merges into the base branch."""
    user_id = _user_id(request)
    root, identity = await _identity(request, body.root)
    try:
        repo = await wt.repo_identity(root, identity)
        return await integration.start(
            user_id=user_id,
            repo_key=repo.key,
            task_ids=body.task_ids,
            run_tests=body.run_tests,
            identity=identity,
        )
    except (registry.CoordinationError, wt.WorktreeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/integrations")
async def list_integrations(request: Request, root: str):
    repo_key = await _repo_key(request, root)
    user_id = _user_id(request)
    runs = [r for r in await CoordIntegration.query(repo_key) if r.user_id == user_id]
    return {"integrations": [r.to_dict() for r in runs]}


@router.get("/integrations/{run_id}")
async def get_integration(request: Request, run_id: str):
    run = await CoordIntegration.get_by_id(run_id)
    if not run or run.user_id != _user_id(request):
        raise HTTPException(404, "integration not found")
    return run.to_dict()


@router.post("/tasks/{task_id}/pr")
async def publish_pr(request: Request, task_id: str, body: PublishRequest):
    """Push the task branch and create or refresh its PR. Requires ``confirm: true``.

    There is no merge endpoint: merging stays in the Git panel, by the user.
    """
    task = await _own_task(request, task_id)
    _, identity = await _identity(request, task.repo_root)
    _require_gh_mutation_allowed(request, identity)
    try:
        result = await prs.ensure_pr(
            task.id, identity=identity, user_confirmed=body.confirm, draft=body.draft
        )
    except (prs.PrError, gh.GhError) as exc:
        raise HTTPException(409, str(exc)) from exc
    await _emit(task.user_id, (await CoordTask.get_by_id(task.id)).to_dict())
    return result


@router.post("/tasks/{task_id}/correction")
async def request_correction(request: Request, task_id: str, body: CorrectionRequest):
    """Ask the task's agent to fix what integration found (starts one agent turn)."""
    task = await _own_task(request, task_id)
    issues = [b for b in (task.blockers or []) if b.get("kind") == "needs_correction"]
    if not issues and not body.note:
        raise HTTPException(409, "nothing to correct")
    if not task.chat_id:
        raise HTTPException(409, "task has no agent chat")
    prompt = "The coordinator needs a correction on this task before it can be integrated:\n"
    prompt += "\n".join(f"- {b.get('detail')} (integration run {b.get('run_id')})" for b in issues)
    if body.note:
        prompt += f"\n\nNote from the user: {body.note}"
    prompt += (
        "\n\nFix it in this worktree only, commit, re-run the tests, then call "
        "coordinate(action='ready') or say you are done."
    )
    try:
        message_id = await scheduler.launch_turn(task.chat_id, task.user_id, prompt)
    except registry.CoordinationError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"message_id": message_id}
