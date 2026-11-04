"""Task validation (tests in the task worktree) and integration readiness checks.

Test commands are configuration, never agent input:

* ``coordination.test_command`` - the full suite, used for integration runs;
* ``coordination.targeted_test_command`` - optional, run for isolated task
  validation; ``{files}`` is replaced by the task's changed files (quoted).

A task may override both via ``meta.test_command`` / ``meta.targeted_test_command``
(set by the user when registering it). Without a targeted command, isolated
validation runs the full command.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import time
from typing import Any

from cptr.models.coordination import CoordConflict, CoordTask, CoordTaskDep
from cptr.utils.config import now_ms
from cptr.utils.coordination import changes as ch
from cptr.utils.coordination import registry
from cptr.utils.coordination import worktrees as wt
from cptr.utils.identity import ExecutionIdentity, env_for, preexec_for

OUTPUT_TAIL = 8000
DEFAULT_TIMEOUT = 900


async def _config(key: str) -> Any:
    from cptr.models import Config

    return await Config.get(key)


async def commands_for(task: CoordTask) -> tuple[str | None, str | None]:
    """Return ``(targeted, full)`` test commands for a task."""
    meta = task.meta or {}
    full = meta.get("test_command") or await _config("coordination.test_command")
    targeted = meta.get("targeted_test_command") or await _config(
        "coordination.targeted_test_command"
    )
    return (str(targeted).strip() or None) if targeted else None, (
        (str(full).strip() or None) if full else None
    )


async def timeout_seconds() -> int:
    try:
        return max(10, int(await _config("coordination.test_timeout") or DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


async def allow_untested() -> bool:
    return (await _config("coordination.allow_untested")) in (True, "true", "1")


async def run_command(
    command: str,
    cwd: str,
    *,
    identity: ExecutionIdentity | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run a configured shell command; returns exit code and the output tail."""
    env = env_for(identity, cwd) if identity and identity.is_pam else os.environ.copy()
    env["CI"] = env.get("CI", "1")
    started = time.monotonic()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        preexec_fn=preexec_for(identity) if identity and identity.is_pam else None,
    )
    timed_out = False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = await proc.communicate()
    text = (out or b"").decode("utf-8", errors="replace")
    return {
        "command": command,
        "exit_code": None if timed_out else proc.returncode,
        "passed": (not timed_out) and proc.returncode == 0,
        "timed_out": timed_out,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "output_tail": text[-OUTPUT_TAIL:],
    }


async def validate_task(task_id: str, identity: ExecutionIdentity | None = None) -> dict:
    """Run the task's tests in its own worktree and record the result on the task."""
    task = await CoordTask.get_by_id(task_id)
    if not task or not task.worktree_path:
        raise registry.CoordinationError("Task has no worktree to validate")
    state = await wt.worktree_state(task.worktree_path, identity)
    if not state.get("exists"):
        raise registry.CoordinationError("Task worktree is missing")
    targeted, full = await commands_for(task)
    result: dict[str, Any] = {
        "head": state.get("head"),
        "dirty": bool(state.get("dirty")),
        "at": now_ms(),
    }
    if targeted:
        cs = await ch.compute(task, identity)
        files = " ".join(shlex.quote(f) for f in sorted(cs.files)) if cs else ""
        command, scope = targeted.replace("{files}", files), "targeted"
    elif full:
        command, scope = full, "full"
    else:
        result.update(status="no_tests", scope=None)
        await _store(task, result)
        return result
    run = await run_command(
        command, task.worktree_path, identity=identity, timeout=await timeout_seconds()
    )
    result.update(run, status="passed" if run["passed"] else "failed", scope=scope)
    await _store(task, result)
    return result


async def _store(task: CoordTask, result: dict) -> None:
    validation = {**(task.validation or {}), "isolated": result}
    await CoordTask.update_fields(
        task.id,
        validation=validation,
        meta=registry._milestone(task.meta, "validated", status=result["status"]),
    )


async def readiness(
    task: CoordTask, candidates: set[str], identity: ExecutionIdentity | None = None
) -> list[str]:
    """Reasons the task cannot be integrated now (empty list = ready to try)."""
    reasons: list[str] = []
    if task.status not in {"reviewing", "integration_ready"}:
        reasons.append(f"status is {task.status} (needs reviewing)")
    if not task.worktree_path or not task.branch:
        return reasons + ["task has no branch/worktree"]
    state = await wt.worktree_state(task.worktree_path, identity)
    if not state.get("exists"):
        return reasons + ["worktree is missing"]
    if state.get("dirty"):
        reasons.append("worktree has uncommitted changes")
    cs = await ch.compute(task, identity)
    if not cs or not cs.has_commits:
        reasons.append("no commits beyond the base")
    isolated = (task.validation or {}).get("isolated") or {}
    if isolated.get("status") == "passed":
        if isolated.get("head") != state.get("head") or isolated.get("dirty"):
            reasons.append("validation is stale (branch changed since tests ran)")
    elif isolated.get("status") == "no_tests" and await allow_untested():
        pass
    elif isolated.get("status") == "failed":
        reasons.append("isolated tests failed")
    else:
        reasons.append("not validated (run validation first)")
    for row in await CoordConflict.query(task.repo_key, statuses={"open"}, task_id=task.id):
        if row.level in {"confirmed", "blocking"} and row.kind != "integration_merge":
            reasons.append(f"unresolved {row.level} conflict ({row.kind})")
    for task_id, dep in await CoordTaskDep.all_for_repo(task.repo_key):
        if task_id != task.id:
            continue
        dep_task = await CoordTask.get_by_id(dep)
        if dep_task and dep_task.status != "completed" and dep not in candidates:
            reasons.append(f"depends on {dep} ({dep_task.status}) which is not in this integration")
    return reasons
