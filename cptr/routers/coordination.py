"""Coordination API: tasks, worktree bindings and their live state."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cptr.models.chats import Chat
from cptr.models.coordination import CoordClaim, CoordTask, CoordTaskDep
from cptr.routers.git import _root_identity
from cptr.utils.coordination import registry
from cptr.utils.coordination import worktrees as wt
from cptr.utils.identity import ExecutionIdentity
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


class BindChatRequest(BaseModel):
    chat_id: str


class StatusRequest(BaseModel):
    status: str
    reason: str | None = None
    expect: str | None = None


class RemoveWorktreeRequest(BaseModel):
    confirm: bool = False


@router.get("/tasks")
async def list_tasks(request: Request, root: str):
    user_id = _user_id(request)
    root, identity = await _identity(request, root)
    try:
        repo = await wt.repo_identity(root, identity)
    except wt.WorktreeError as exc:
        raise HTTPException(400, str(exc)) from exc
    tasks = await CoordTask.query(user_id=user_id, repo_key=repo.key)
    deps = await CoordTaskDep.all_for_repo(repo.key)
    return {
        "repo_root": repo.main_root,
        "tasks": [t.to_dict() for t in tasks],
        "dependencies": [{"task_id": a, "depends_on": b} for a, b in deps],
    }


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
    if body.chat_id:
        chat = await Chat.get_by_id(body.chat_id)
        if not chat or chat.user_id != user_id:
            raise HTTPException(404, "chat not found")
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
            identity=identity,
        )
    except (registry.CoordinationError, wt.WorktreeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    await _emit(user_id, result["task"])
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
    return result


@router.post("/tasks/{task_id}/worktree/remove")
async def remove_worktree(request: Request, task_id: str, body: RemoveWorktreeRequest):
    """Remove a task's worktree (branch kept). Requires ``confirm: true`` from the user."""
    task = await _own_task(request, task_id)
    if not task.worktree_path:
        raise HTTPException(400, "task has no worktree")
    if task.status not in {"completed", "failed", "paused", "blocked"}:
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
