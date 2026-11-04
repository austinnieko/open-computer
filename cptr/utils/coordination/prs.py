"""GitHub PR ownership for tasks: publish or update, never merge.

``ensure_pr`` is idempotent: it reuses the PR recorded on the task, else any
existing PR whose head is the task branch, and only then pushes the branch
and creates one. It requires an explicit user action, because pushing and
opening a PR are outward-facing.

There is deliberately no merge function. ``_gh`` refuses ``pr merge`` and
other destructive subcommands outright so no coordinator code path can merge
or close anything; merging stays with the user in the Git panel.
"""

from __future__ import annotations

import json
from typing import Any

from cptr.models.coordination import CoordConflict, CoordIntegration, CoordTask, CoordTaskDep
from cptr.utils import gh
from cptr.utils.coordination import changes as ch
from cptr.utils.coordination import registry
from cptr.utils.git import _run, push
from cptr.utils.identity import ExecutionIdentity

_FORBIDDEN = {("pr", "merge"), ("pr", "close"), ("repo", "delete")}
PR_FIELDS = "number,url,state,isDraft,headRefName,baseRefName"


class PrError(Exception):
    pass


async def _gh(args: list[str], *, identity: ExecutionIdentity, cwd: str, check: bool = True):
    if tuple(args[:2]) in _FORBIDDEN:
        raise PrError(f"The coordinator never runs 'gh {' '.join(args[:2])}'")
    return await gh.run_gh(args, identity=identity, cwd=cwd, check=check, timeout=60)


async def find_existing(task: CoordTask, identity: ExecutionIdentity) -> dict | None:
    if task.pr_number:
        code, out, _ = await _gh(
            ["pr", "view", str(task.pr_number), "--json", PR_FIELDS],
            identity=identity,
            cwd=task.repo_root,
            check=False,
        )
        if code == 0 and out.strip():
            return json.loads(out)
    _, out, _ = await _gh(
        [
            "pr",
            "list",
            "--head",
            task.branch,
            "--state",
            "all",
            "--json",
            PR_FIELDS,
            "--limit",
            "5",
        ],
        identity=identity,
        cwd=task.repo_root,
    )
    items = [p for p in json.loads(out or "[]") if p.get("headRefName") == task.branch]
    open_items = [p for p in items if p.get("state") == "OPEN"]
    return (open_items or items or [None])[0]


async def pr_body(task: CoordTask, identity: ExecutionIdentity | None = None) -> str:
    lines = [
        task.description or task.title,
        "",
        "## Coordination",
        f"- Task: `{task.id}`",
        f"- Branch: `{task.branch}` (base `{(task.meta or {}).get('base_branch')}`)",
    ]
    deps = [d for t, d in await CoordTaskDep.all_for_repo(task.repo_key) if t == task.id]
    for dep_id in deps:
        dep = await CoordTask.get_by_id(dep_id)
        if dep:
            ref = f"#{dep.pr_number}" if dep.pr_number else f"`{dep.branch}` (no PR yet)"
            lines.append(f"- Depends on: {dep.title} — {ref}")
    isolated = (task.validation or {}).get("isolated") or {}
    if isolated:
        lines.append(
            f"- Isolated tests: {isolated.get('status')}"
            + (f" (`{isolated.get('command')}`)" if isolated.get("command") else "")
        )
    integ = (task.validation or {}).get("integration") or {}
    if integ.get("run_id"):
        run = await CoordIntegration.get_by_id(integ["run_id"])
        if run:
            lines.append(f"- Integration run `{run.id}`: {run.summary}")
    conflicts = [
        c
        for c in await CoordConflict.query(task.repo_key, task_id=task.id)
        if c.status == "acknowledged"
    ]
    for c in conflicts:
        lines.append(f"- Acknowledged {c.level} conflict: {c.kind}")
    cs = await ch.compute(task, identity)
    if cs and cs.files:
        files = sorted(cs.files)
        lines.append(
            f"- Files changed ({len(files)}): "
            + ", ".join(f"`{f}`" for f in files[:20])
            + (" …" if len(files) > 20 else "")
        )
    lines += ["", "_Opened by the Open Computer coordinator. Merging is left to a human._"]
    return "\n".join(lines)


async def ensure_pr(
    task_id: str,
    *,
    identity: ExecutionIdentity,
    user_confirmed: bool,
    draft: bool = False,
) -> dict[str, Any]:
    """Create or refresh the task's PR. Returns ``{"pr": {...}, "created": bool}``."""
    if not user_confirmed:
        raise PrError("Publishing a PR requires explicit user approval")
    task = await CoordTask.get_by_id(task_id)
    if not task or not task.branch:
        raise PrError("Task has no branch")
    if task.status not in {"integration_ready", "completed"}:
        raise PrError(f"Task is {task.status}; only integration-ready tasks are published")

    existing = await find_existing(task, identity)
    created = False
    if existing is None:
        code, _, _ = await _run(
            "remote", "get-url", "origin", cwd=task.repo_root, check=False, identity=identity
        )
        if code != 0:
            raise PrError("Repository has no 'origin' remote")
        pushed = await push(
            task.worktree_path or task.repo_root,
            set_upstream=True,
            branch=task.branch,
            identity=identity,
        )
        if not pushed["ok"]:
            raise PrError(f"git push failed: {pushed['message'][-500:]}")
        args = [
            "pr",
            "create",
            "--head",
            task.branch,
            "--title",
            task.title,
            "--body",
            await pr_body(task, identity),
        ]
        base = (task.meta or {}).get("base_branch")
        if base:
            args += ["--base", base]
        if draft:
            args.append("--draft")
        _, out, _ = await _gh(args, identity=identity, cwd=task.worktree_path or task.repo_root)
        url = out.strip().splitlines()[-1] if out.strip() else ""
        _, view, _ = await _gh(
            ["pr", "view", url or task.branch, "--json", PR_FIELDS],
            identity=identity,
            cwd=task.repo_root,
        )
        existing = json.loads(view)
        created = True
    else:
        # Keep the branch on GitHub in sync with the integrated head (fast-forward only).
        pushed = await push(
            task.worktree_path or task.repo_root, branch=task.branch, identity=identity
        )
        existing["push"] = "ok" if pushed["ok"] else pushed["message"][-300:]

    meta = registry._milestone(task.meta, "pr_published", number=existing.get("number"))
    await CoordTask.update_fields(
        task.id,
        pr_number=existing.get("number"),
        pr_url=existing.get("url"),
        pr_state=existing.get("state"),
        meta=meta,
    )
    return {"pr": existing, "created": created}
