"""Coordination API: tasks, worktree bindings and their live state."""

from __future__ import annotations

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
    start_prompt: str | None = None
    model_id: str | None = None
    test_command: str | None = None
    targeted_test_command: str | None = None


class BindChatRequest(BaseModel):
    chat_id: str


class StatusRequest(BaseModel):
    status: str
    reason: str | None = None
    expect: str | None = None


class RemoveWorktreeRequest(BaseModel):
    confirm: bool = False


class DependencyRequest(BaseModel):
    depends_on: str


class MessageRequest(BaseModel):
    kind: str = "note"
    body: str = Field(min_length=1, max_length=2000)
    files: list[str] = []


class ConflictStatusRequest(BaseModel):
    status: str  # acknowledged | resolved | open


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
            start_prompt=body.start_prompt,
            model_id=body.model_id,
            test_command=body.test_command,
            targeted_test_command=body.targeted_test_command,
            identity=identity,
        )
    except (registry.CoordinationError, wt.WorktreeError) as exc:
        raise HTTPException(409, str(exc)) from exc
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


async def _repo_key(request: Request, root: str) -> str:
    root, identity = await _identity(request, root)
    try:
        return (await wt.repo_identity(root, identity)).key
    except wt.WorktreeError as exc:
        raise HTTPException(400, str(exc)) from exc


async def _owned_repo(request: Request, repo_key: str) -> None:
    user_id = _user_id(request)
    if not await CoordTask.query(user_id=user_id, repo_key=repo_key):
        raise HTTPException(404, "no tasks for this repository")


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


@router.get("/conflicts")
async def list_conflicts(request: Request, root: str, include_resolved: bool = False):
    repo_key = await _repo_key(request, root)
    await _owned_repo(request, repo_key)
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
    await _owned_repo(request, repo_key)
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
