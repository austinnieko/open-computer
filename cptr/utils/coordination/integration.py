"""Coordinator-controlled integration of finished task branches.

1. Check each candidate (clean, committed, validated at its current head, no
   unresolved confirmed/blocking conflicts, dependencies included or done).
2. Order them by the dependency graph (then by age).
3. Create a fresh integration worktree on ``coord/integration-<run>`` from
   the base branch head. Task branches are never modified or rewritten.
4. Merge each task's validated head with ``--no-ff`` so every task stays a
   traceable merge commit; a merge conflict aborts that merge, records the
   evidence and skips the task (and anything depending on it).
5. Run the full test command on the combined result. On failure, re-run it at
   each merge step to find the first task whose merge breaks the build.
6. Tasks that merged and passed become ``integration_ready``. Tasks that need
   changes become ``blocked`` with a ``needs_correction`` blocker and a
   message to their agent.

Never merges into the base branch, never pushes, never deletes the
integration worktree. Only one integration per repository runs at a time
(an exclusive claim).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cptr.models.coordination import (
    ClaimConflict,
    CoordClaim,
    CoordConflict,
    CoordIntegration,
    CoordTask,
    CoordTaskDep,
)
from cptr.utils.config import now_ms
from cptr.utils.coordination import changes as ch
from cptr.utils.coordination import messages, registry, validation
from cptr.utils.coordination import worktrees as wt
from cptr.utils.git import _run
from cptr.utils.identity import ExecutionIdentity

logger = logging.getLogger(__name__)

_runs: dict[str, asyncio.Task] = {}
_COMMITTER = {
    "GIT_AUTHOR_NAME": "Open Computer Coordinator",
    "GIT_AUTHOR_EMAIL": "coordinator@open-computer.invalid",
    "GIT_COMMITTER_NAME": "Open Computer Coordinator",
    "GIT_COMMITTER_EMAIL": "coordinator@open-computer.invalid",
}


def _claim_resource() -> str:
    return "integration:active"


def topo_order(
    task_ids: list[str], edges: list[tuple[str, str]], created: dict[str, int]
) -> list[str]:
    """Dependencies first; ties broken by creation time. Only edges inside the set count."""
    ids = set(task_ids)
    pending = {t: {d for a, d in edges if a == t and d in ids} for t in ids}
    order: list[str] = []
    while pending:
        ready = sorted(
            (t for t, deps in pending.items() if not deps), key=lambda t: (created[t], t)
        )
        if not ready:  # cycles are rejected when edges are added; be defensive anyway
            ready = sorted(pending, key=lambda t: (created[t], t))[:1]
        for t in ready:
            order.append(t)
            pending.pop(t)
        for deps in pending.values():
            deps.difference_update(ready)
    return order


async def _git_env(cwd: str, identity) -> dict[str, str]:
    code, out, _ = await _run("config", "user.email", cwd=cwd, check=False, identity=identity)
    return {} if code == 0 and out.strip() else dict(_COMMITTER)


async def start(
    *,
    user_id: str,
    repo_key: str,
    task_ids: list[str] | None = None,
    run_tests: bool = True,
    identity: ExecutionIdentity | None = None,
) -> dict[str, Any]:
    """Validate candidates, create the run record and start it in the background."""
    tasks = await CoordTask.query(user_id=user_id, repo_key=repo_key)
    if not tasks:
        raise registry.CoordinationError("No tasks in this repository")
    by_id = {t.id: t for t in tasks}
    if task_ids:
        unknown = [t for t in task_ids if t not in by_id]
        if unknown:
            raise registry.CoordinationError(f"Unknown tasks: {', '.join(unknown)}")
        candidates = [by_id[t] for t in task_ids]
    else:
        candidates = [t for t in tasks if t.status in {"reviewing", "integration_ready"}]
    if not candidates:
        raise registry.CoordinationError("No tasks are ready for integration")

    excluded: dict[str, list[str]] = {}
    for task in candidates:
        reasons = await validation.readiness(task, {c.id for c in candidates}, identity)
        if reasons:
            excluded[task.id] = reasons
    edges = await CoordTaskDep.all_for_repo(repo_key)
    changed = True
    while changed:  # a task whose in-set dependency was excluded is excluded too
        changed = False
        for task_id, dep in edges:
            if (
                task_id in {c.id for c in candidates}
                and task_id not in excluded
                and dep in excluded
            ):
                excluded[task_id] = [f"depends on {dep}, which cannot be integrated"]
                changed = True
    included = [c for c in candidates if c.id not in excluded]
    bases = {(c.meta or {}).get("base_branch") for c in included}
    if len(bases) > 1:
        raise registry.CoordinationError(
            f"Tasks target different base branches: {sorted(map(str, bases))}"
        )
    base_branch = bases.pop() if bases else None
    if included and not base_branch:
        raise registry.CoordinationError("Cannot determine the base branch to integrate into")

    order = topo_order([c.id for c in included], edges, {c.id: c.created_at for c in included})
    run = await CoordIntegration.create(
        user_id=user_id,
        repo_key=repo_key,
        status="running",
        base_branch=base_branch,
        task_order=order,
        steps=[{"task_id": t, "result": "excluded", "reasons": r} for t, r in excluded.items()],
    )
    if not order:
        await CoordIntegration.update_fields(
            run.id, status="failed", summary="No candidate passed the readiness checks"
        )
        return (await CoordIntegration.get_by_id(run.id)).to_dict()
    try:
        await CoordClaim.claim(repo_key, run.id, "integration", _claim_resource())
    except ClaimConflict as exc:
        await CoordIntegration.update_fields(
            run.id, status="failed", summary=f"Another integration is running ({exc.owner_task_id})"
        )
        raise registry.CoordinationError(
            f"Another integration is running: {exc.owner_task_id}"
        ) from exc

    root = by_id[order[0]].repo_root
    _runs[run.id] = asyncio.create_task(_execute(run.id, root, identity, run_tests))
    return (await CoordIntegration.get_by_id(run.id)).to_dict()


async def wait(run_id: str) -> dict:
    task = _runs.get(run_id)
    if task:
        await asyncio.gather(task, return_exceptions=True)
    return (await CoordIntegration.get_by_id(run_id)).to_dict()


async def _needs_correction(
    task_id: str, run_id: str, detail: str, body: str, files: list[str] | None = None
) -> None:
    task = await CoordTask.get_by_id(task_id)
    if not task:
        return
    blockers = [b for b in (task.blockers or []) if b.get("kind") != "needs_correction"]
    blockers.append(
        {"kind": "needs_correction", "detail": detail, "run_id": run_id, "since": now_ms()}
    )
    meta = dict(task.meta or {})
    if task.status != "blocked":
        meta["status_before_block"] = task.status
    await CoordTask.update_fields(task.id, status="blocked", blockers=blockers, meta=meta)
    await messages.post(
        from_task=None,
        repo_key=task.repo_key,
        to_task=task.id,
        kind="incompatibility",
        body=body,
        files=files,
        payload={"run_id": run_id},
    )


async def _clear_correction(task: CoordTask) -> dict[str, Any]:
    blockers = [b for b in (task.blockers or []) if b.get("kind") != "needs_correction"]
    fields: dict[str, Any] = {"blockers": blockers}
    if task.status == "blocked" and not blockers:
        fields["status"] = "reviewing"
    return fields


async def _execute(run_id: str, root: str, identity, run_tests: bool) -> None:
    run = await CoordIntegration.get_by_id(run_id)
    steps: list[dict] = list(run.steps or [])
    try:
        repo = await wt.repo_identity(root, identity)
        base_sha = await wt.resolve_sha(repo.main_root, f"refs/heads/{run.base_branch}", identity)
        if not base_sha:
            raise registry.CoordinationError(f"Base branch {run.base_branch} not found")
        branch = f"coord/integration-{run_id[4:]}"
        path = wt.default_worktree_path(repo.main_root, f"integration-{run_id[4:]}")
        await wt.ensure_worktree(
            repo, branch=branch, path=path, base_ref=base_sha, identity=identity
        )
        await CoordIntegration.update_fields(
            run_id, base_sha=base_sha, branch=branch, worktree_path=path
        )
        env = await _git_env(path, identity)

        merged: list[dict] = []
        skipped: set[str] = set()
        edges = await CoordTaskDep.all_for_repo(run.repo_key)
        for task_id in run.task_order or []:
            task = await CoordTask.get_by_id(task_id)
            deps_skipped = [d for a, d in edges if a == task_id and d in skipped]
            if deps_skipped:
                skipped.add(task_id)
                steps.append(
                    {
                        "task_id": task_id,
                        "result": "skipped",
                        "reasons": [f"dependency {d} did not integrate" for d in deps_skipped],
                    }
                )
                continue
            head = ((task.validation or {}).get("isolated") or {}).get("head")
            if not head:
                cs = await ch.compute(task, identity)
                head = cs.head if cs else task.branch
            code, out, err = await _run(
                "merge",
                "--no-ff",
                "--no-edit",
                "-m",
                f"Integrate {task.id}: {task.title}\n\nBranch: {task.branch}\nHead: {head}",
                head,
                cwd=path,
                check=False,
                identity=identity,
                extra_env=env,
            )
            if code == 0:
                _, commit, _ = await _run("rev-parse", "HEAD", cwd=path, identity=identity)
                merged.append(
                    {"task_id": task_id, "result": "merged", "commit": commit.strip(), "head": head}
                )
                steps.append(merged[-1])
                continue
            _, conflicted, _ = await _run(
                "diff", "--name-only", "--diff-filter=U", cwd=path, check=False, identity=identity
            )
            files = sorted(f for f in conflicted.splitlines() if f.strip())
            await _run("merge", "--abort", cwd=path, check=False, identity=identity)
            skipped.add(task_id)
            culprits = []
            for prior in merged:
                prior_task = await CoordTask.get_by_id(prior["task_id"])
                cs = await ch.compute(prior_task, identity) if prior_task else None
                if cs and set(files) & cs.files:
                    culprits.append(prior["task_id"])
            steps.append(
                {
                    "task_id": task_id,
                    "result": "conflict",
                    "files": files,
                    "with": culprits,
                    "detail": (err or out).strip()[-2000:],
                }
            )
            for other in culprits or [None]:
                await CoordConflict.upsert(
                    repo_key=run.repo_key,
                    task_a=task_id,
                    task_b=other,
                    level="confirmed",
                    kind="integration_merge",
                    evidence={"subject": f"run:{run_id}", "files": files, "run_id": run_id},
                    fingerprint=f"integration:{run_id}:{task_id}:{other}",
                )
            await _needs_correction(
                task_id,
                run_id,
                "merge conflict during integration",
                f"Integration run {run_id} could not merge your branch: conflicts in "
                f"{', '.join(files) or 'unknown files'}"
                + (f" with {', '.join(culprits)}" if culprits else "")
                + f". Merge {run.base_branch} and the listed task branches into your branch, "
                "resolve, re-run tests and report ready again.",
                files,
            )

        tests: dict[str, Any] | None = None
        passed_ids: list[str] = []
        _, full = (
            await validation.commands_for(await CoordTask.get_by_id(merged[0]["task_id"]))
            if merged
            else (None, None)
        )
        if merged and run_tests and full:
            timeout = await validation.timeout_seconds()
            tests = await validation.run_command(full, path, identity=identity, timeout=timeout)
            if tests["passed"]:
                passed_ids = [m["task_id"] for m in merged]
            else:
                culprit = None
                for index, step in enumerate(merged):
                    if index == len(merged) - 1:
                        culprit = step["task_id"]
                        break
                    await _run(
                        "checkout", "-q", "--detach", step["commit"], cwd=path, identity=identity
                    )
                    step_run = await validation.run_command(
                        full, path, identity=identity, timeout=timeout
                    )
                    if not step_run["passed"]:
                        culprit = step["task_id"]
                        break
                    passed_ids.append(step["task_id"])
                await _run("checkout", "-q", branch, cwd=path, check=False, identity=identity)
                tests["attributed_to"] = culprit
                if culprit:
                    first_bad = next(i for i, m in enumerate(merged) if m["task_id"] == culprit)
                    culprit_task = await CoordTask.get_by_id(culprit)
                    combined_with = [m["task_id"] for m in merged[:first_bad]]
                    await _needs_correction(
                        culprit,
                        run_id,
                        "integration tests failed",
                        f"Integration run {run_id}: the combined tests fail once your branch is "
                        f"merged on top of {', '.join(combined_with) or run.base_branch}. "
                        f"Test output (tail):\n{tests['output_tail'][-1500:]}",
                        sorted((await ch.compute(culprit_task, identity)).files)
                        if culprit_task
                        else None,
                    )
                    for m in merged[first_bad + 1 :]:
                        steps_entry = next(s for s in steps if s.get("task_id") == m["task_id"])
                        steps_entry["note"] = "not verified: an earlier merge broke the tests"
        elif merged and await validation.allow_untested():
            passed_ids = [m["task_id"] for m in merged]

        for task_id in passed_ids:
            for row in await CoordConflict.query(
                run.repo_key, statuses={"open", "acknowledged"}, task_id=task_id
            ):
                if row.kind == "integration_merge" and row.task_a == task_id:
                    await CoordConflict.set_status(row.id, "resolved")
            task = await CoordTask.get_by_id(task_id)
            fields = await _clear_correction(task)
            fields["validation"] = {
                **(task.validation or {}),
                "integration": {
                    "run_id": run_id,
                    "passed": True,
                    "head": next(m["head"] for m in merged if m["task_id"] == task_id),
                },
            }
            status = fields.get("status", task.status)
            if status in {"reviewing", "integration_ready"}:
                fields["status"] = "integration_ready"
                fields["meta"] = registry._milestone(task.meta, "integration_ready", run_id=run_id)
            await CoordTask.update_fields(task.id, **fields)

        if not merged:
            status, summary = "failed", "No task branch could be merged"
        elif tests is None:
            status = "passed" if passed_ids else "failed"
            summary = f"Merged {len(merged)} task(s); " + (
                "no test command configured (allowed)"
                if passed_ids
                else "no test command configured, so nothing was marked integration-ready"
            )
        else:
            status = (
                "passed"
                if tests["passed"] and len(merged) == len(run.task_order or [])
                else "failed"
            )
            summary = f"Merged {len(merged)}/{len(run.task_order or [])}; tests " + (
                "passed"
                if tests["passed"]
                else f"failed (first failing: {tests.get('attributed_to')})"
            )
        await CoordIntegration.update_fields(
            run_id, status=status, steps=steps, tests=tests, summary=summary
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[coord] integration %s failed", run_id)
        await CoordIntegration.update_fields(
            run_id, status="failed", steps=steps, summary=f"Integration error: {exc}"
        )
    finally:
        await CoordClaim.release(run_id)
        _runs.pop(run_id, None)
        await _after_run(run_id)


async def _after_run(run_id: str) -> None:
    from cptr.utils.coordination import coordinator

    run = await CoordIntegration.get_by_id(run_id)
    if not run:
        return
    await coordinator._emit({run.user_id}, {"type": "coord:integration", "run": run.to_dict()})
    coordinator.request_refresh(run.repo_key)


async def reconcile() -> int:
    """Runs left ``running`` by a restart become ``interrupted`` and free the lock.

    The partial integration worktree is kept for inspection; a new run uses a
    new worktree, so nothing is repeated on top of a half-finished merge.
    """
    count = 0
    for run in await CoordIntegration.query(status="running", limit=500):
        if run.id in _runs:
            continue
        await CoordIntegration.update_fields(
            run.id, status="interrupted", summary="Interrupted by a restart; start a new run"
        )
        await CoordClaim.release(run.id)
        count += 1
    return count
