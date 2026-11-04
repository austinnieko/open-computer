"""Task lifecycle: how an agent checks in and out, and how work is handed on.

Stages (what the dashboard shows) map onto the stored statuses:

* Draft    - ``queued``, never started and no pending start prompt.
* Active   - ``running`` (Working), ``queued`` with a start prompt (In line),
             ``queued`` + ``meta.waiting_for_user`` (Needs you), ``blocked``,
             ``paused`` (Stopped) or ``failed``.
* Review   - ``reviewing`` / ``integration_ready``.
* Done     - ``completed``; ``ended`` is the give-up exit.

Check-out: every agent turn must end with one line the coordinator can read,
whatever the agent (built-in model, Claude Code, OpenCode, ...)::

    CHECKOUT: done - <what changed, what's left, what to watch out for>
    CHECKOUT: needs-input - <the question for the user>
    CHECKOUT: blocked - <what it is blocked on>
    CHECKOUT: continuing - <what it is still doing, e.g. waiting on sub-agents>

Handoff: when a task's dependencies are ready, its folder is prepared from
their branches (merged in when that is clean) and its first brief carries each
dependency's check-out note, branch and changed files.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from cptr.models.coordination import CoordTask, CoordTaskDep
from cptr.utils.config import now_ms

logger = logging.getLogger(__name__)

CHECKOUT_KINDS = ("done", "needs-input", "blocked", "continuing")
_CHECKOUT_RE = re.compile(
    r"^[\s>*_`#-]*CHECKOUT\s*:\s*(done|needs[-_ ]input|blocked|continuing)\b[\s*_`]*"
    r"(?:[-–—:]\s*)?(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

CHECKOUT_INSTRUCTIONS = (
    "\n\n[Coordinator] End every reply with exactly one check-out line, on its own line:\n"
    "CHECKOUT: done - <what you changed, what is left, anything the next agent must know>\n"
    "CHECKOUT: needs-input - <the question you need the user to answer>\n"
    "CHECKOUT: blocked - <what you are blocked on>\n"
    "CHECKOUT: continuing - <what is still running, e.g. sub-agents you are waiting on>\n"
    "Use done only when the task is finished and committed on your branch."
)


def parse_checkout(text: str) -> dict[str, str] | None:
    """The last check-out line in ``text`` as ``{"kind", "note"}``, or None."""
    if not text:
        return None
    matches = list(_CHECKOUT_RE.finditer(text))
    if not matches:
        return None
    kind, note = matches[-1].group(1), matches[-1].group(2)
    kind = kind.lower().replace("_", "-").replace(" ", "-")
    return {"kind": kind, "note": note.strip().strip("*_`").strip()[:2000]}


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


async def last_reply_checkout(chat_id: str) -> dict[str, str] | None:
    """The check-out in the chat's latest finished assistant reply."""
    from cptr.models import ChatMessage

    messages = await ChatMessage.get_all_by_chat(chat_id)
    for message in reversed(messages):
        if message.role != "assistant":
            continue
        if not message.done:
            return None
        return parse_checkout(_message_text(message.content))
    return None


# ── Handoff ─────────────────────────────────────────────────


async def dependencies_of(task: CoordTask) -> list[CoordTask]:
    pairs = await CoordTaskDep.all_for_repo(task.repo_key)
    ids = [dep for tid, dep in pairs if tid == task.id]
    found = [await CoordTask.get_by_id(dep_id) for dep_id in ids]
    return [d for d in found if d is not None]


async def prepare_from_dependencies(task: CoordTask, identity=None) -> list[dict]:
    """Bring finished dependencies' work into the task's folder before it starts.

    Only when the folder is clean: each dependency branch not already contained
    is merged in (``--no-edit``). A merge that conflicts is aborted and reported,
    so the agent can do it by hand. Nothing outside the task's own worktree is
    touched. Returns one result per dependency.
    """
    from cptr.utils.git import _run

    deps = [d for d in await dependencies_of(task) if d.branch]
    if not deps or not task.worktree_path:
        return []
    results: list[dict] = []
    code, status, _ = await _run(
        "status", "--porcelain", cwd=task.worktree_path, check=False, identity=identity
    )
    if code != 0 or status.strip():
        return [{"task": d.id, "branch": d.branch, "result": "skipped_dirty_folder"} for d in deps]
    for dep in deps:
        code, _, _ = await _run(
            "merge-base",
            "--is-ancestor",
            dep.branch,
            "HEAD",
            cwd=task.worktree_path,
            check=False,
            identity=identity,
        )
        if code == 0:
            results.append({"task": dep.id, "branch": dep.branch, "result": "already_included"})
            continue
        code, _, err = await _run(
            "-c",
            "user.name=Open Computer",
            "-c",
            "user.email=coordinator@open-computer.local",
            "merge",
            "--no-edit",
            dep.branch,
            cwd=task.worktree_path,
            check=False,
            identity=identity,
        )
        if code == 0:
            results.append({"task": dep.id, "branch": dep.branch, "result": "merged"})
        else:
            await _run("merge", "--abort", cwd=task.worktree_path, check=False, identity=identity)
            results.append(
                {
                    "task": dep.id,
                    "branch": dep.branch,
                    "result": "conflict",
                    "detail": (err or "").strip()[:300],
                }
            )
    return results


async def ensure_dependency_merges(task: CoordTask) -> CoordTask:
    """Once per task, before its first turn: merge in its ready dependencies' work.

    Runs only when every dependency is ready (``READY`` statuses), records the
    results in ``meta.dependency_merges`` and a ``started_after`` milestone, and
    returns the refreshed task. Failures never stop the task from starting.
    """
    from cptr.utils.coordination import scheduler
    from cptr.utils.identity import identity_for_user_id

    if (task.meta or {}).get("dependency_merges") is not None or not task.worktree_path:
        return task
    deps = await dependencies_of(task)
    if not deps or any(d.status not in scheduler.READY for d in deps):
        return task
    try:
        identity = await identity_for_user_id(task.user_id)
        results = await prepare_from_dependencies(task, identity)
    except Exception as exc:  # noqa: BLE001 - never block the start
        logger.warning("[coord] preparing %s from dependencies failed: %s", task.id, exc)
        results = [{"task": d.id, "branch": d.branch, "result": "error"} for d in deps]
    fresh = await CoordTask.get_by_id(task.id) or task
    meta = dict(fresh.meta or {})
    meta["dependency_merges"] = results
    milestones = dict(meta.get("milestones") or {})
    milestones["started_after"] = {"at": now_ms(), "tasks": [d.id for d in deps]}
    meta["milestones"] = milestones
    await CoordTask.update_fields(task.id, meta=meta)
    return await CoordTask.get_by_id(task.id) or fresh


async def handoff_brief(task: CoordTask) -> str:
    """What the task's agent needs from the tasks it depends on."""
    deps = await dependencies_of(task)
    if not deps:
        return ""
    merges = {m.get("task"): m for m in (task.meta or {}).get("dependency_merges") or []}
    lines = ["", "", "[Coordinator] This task builds on the work of:"]
    for dep in deps:
        lines.append(f"- {dep.title} ({dep.id}), branch {dep.branch or '-'}, status {dep.status}")
        checkout = (dep.meta or {}).get("checkout") or {}
        if checkout.get("note"):
            lines.append(f"  Handoff note: {checkout['note']}")
        files = (dep.meta or {}).get("observed_files") or dep.files or []
        if files:
            shown = ", ".join(files[:30]) + (
                f" (+{len(files) - 30} more)" if len(files) > 30 else ""
            )
            lines.append(f"  Files it changed: {shown}")
        merge = merges.get(dep.id, {}).get("result")
        if merge in {"merged", "already_included"}:
            lines.append("  Its work is already in your folder.")
        elif merge == "conflict":
            lines.append(
                f"  Merging {dep.branch} into your branch conflicted. Merge it yourself first "
                "and resolve the conflicts before other changes."
            )
        elif merge == "skipped_dirty_folder":
            lines.append(
                f"  Your folder had changes, so {dep.branch} was not merged in. Merge it first."
            )
    return "\n".join(lines)


def checkout_meta(meta: dict | None, checkout: dict[str, str] | None) -> dict:
    """Task meta with the latest check-out recorded (None = the agent did not report)."""
    out = dict(meta or {})
    out["checkout"] = {**(checkout or {"kind": "none", "note": ""}), "at": now_ms()}
    return out
