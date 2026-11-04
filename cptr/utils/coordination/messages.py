"""Structured coordination messages between tasks.

Agents do not share memory. A message is stored per recipient and handed to
that agent at the start of its next turn as a compact ``[Coordinator]`` block
appended to the prompt. Nothing here starts a model turn by itself, so
messaging never adds inference load.

Relevance: a message without an explicit recipient goes only to active tasks
that touch one of the message's files or are directly linked to the sender
by a dependency.
"""

from __future__ import annotations

from typing import Any

from cptr.models.coordination import ACTIVE_STATUSES, CoordMessage, CoordTask, CoordTaskDep
from cptr.utils.coordination import changes as ch

KINDS = {
    "interface_change",  # "I am changing the detection API."
    "ready",  # "The new configuration schema is ready."
    "depends_on",  # "My changes depend on your branch."
    "file_overlap",  # "We are both modifying the same file."
    "integration_ready",  # "My implementation is ready for integration."
    "incompatibility",  # "Testing found an incompatibility with your changes."
    "conflict",  # coordinator-generated conflict notice
    "note",
}
MAX_BODY = 2000
MAX_PER_DELIVERY = 8


class MessageError(Exception):
    pass


async def _recipients(sender: CoordTask, files: list[str]) -> list[str]:
    tasks = await CoordTask.query(repo_key=sender.repo_key, statuses=ACTIVE_STATUSES)
    linked = {
        b if a == sender.id else a
        for a, b in await CoordTaskDep.all_for_repo(sender.repo_key)
        if sender.id in (a, b)
    }
    wanted = set(files)
    out = []
    for task in tasks:
        if task.id == sender.id:
            continue
        touched = set(task.files or [])
        cached = ch._cache.get(task.id)
        if cached:
            touched |= cached.files
        if task.id in linked or (wanted and touched & wanted):
            out.append(task.id)
    return out


async def post(
    *,
    from_task: str | None,
    kind: str,
    body: str,
    to_task: str | None = None,
    files: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    repo_key: str | None = None,
) -> list[dict]:
    """Queue a message. Returns the stored rows (one per recipient)."""
    if kind not in KINDS:
        raise MessageError(f"Unknown message kind {kind!r}")
    body = (body or "").strip()[:MAX_BODY]
    if not body:
        raise MessageError("Message body is required")
    sender = await CoordTask.get_by_id(from_task) if from_task else None
    if from_task and not sender:
        raise MessageError(f"Unknown task {from_task}")
    repo_key = sender.repo_key if sender else repo_key
    if to_task:
        target = await CoordTask.get_by_id(to_task)
        if not target or target.repo_key != repo_key:
            raise MessageError(f"Unknown task {to_task} in this repository")
        recipients = [to_task]
    elif sender:
        recipients = await _recipients(sender, files or [])
    else:
        raise MessageError("A message needs a sender or a recipient")
    data = {**(payload or {}), **({"files": sorted(set(files))} if files else {})}
    rows = []
    for recipient in recipients:
        rows.append(
            await CoordMessage.create(
                repo_key=repo_key,
                from_task=from_task,
                to_task=recipient,
                kind=kind,
                body=body,
                payload=data or None,
            )
        )
    return [r.to_dict() for r in rows]


def format_block(messages: list[CoordMessage], titles: dict[str, str]) -> str:
    lines = ["", "", "[Coordinator] Updates from other agents working in this repository:"]
    for m in messages:
        who = titles.get(m.from_task or "", "coordinator") if m.from_task else "coordinator"
        files = (m.payload or {}).get("files")
        suffix = f" (files: {', '.join(files[:6])})" if files else ""
        lines.append(f"- [{m.kind}] from {who}: {m.body}{suffix}")
    lines.append(
        "Take these into account. Do not edit another task's worktree; coordinate through "
        "your own branch."
    )
    return "\n".join(lines)


async def take_delivery(task_id: str) -> str:
    """Claim this task's undelivered messages and return the prompt block ('' if none).

    Duplicates (same sender, kind and body) are collapsed, and at most
    ``MAX_PER_DELIVERY`` messages are delivered per turn; the rest wait.
    """
    pending = await CoordMessage.pending_for(task_id)
    if not pending:
        return ""
    unique: dict[tuple, CoordMessage] = {}
    dupes: list[str] = []
    for m in pending:
        key = (m.from_task, m.kind, m.body)
        if key in unique:
            dupes.append(m.id)
        else:
            unique[key] = m
    batch = list(unique.values())[:MAX_PER_DELIVERY]
    claimed = await CoordMessage.mark_delivered([m.id for m in batch] + dupes)
    delivered = [m for m in batch if m.id in claimed]
    if not delivered:
        return ""
    titles = {}
    for m in delivered:
        if m.from_task and m.from_task not in titles:
            sender = await CoordTask.get_by_id(m.from_task)
            titles[m.from_task] = f'"{sender.title}" ({sender.id})' if sender else m.from_task
    return format_block(delivered, titles)
