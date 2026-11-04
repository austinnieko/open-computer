"""Conflict detection between active tasks in one repository.

Evidence comes from Git and task metadata only:

* change sets (``changes.compute``): overlapping files, shared config,
  lockfiles, parallel migrations, public-interface edits, dependency specs;
* ``git merge-tree --write-tree`` between task branches (and against the
  moved base branch) for *confirmed* textual conflicts;
* the dependency graph (unfinished or failed prerequisites);
* titles and declared files (possible duplicate work).

Levels: ``info`` < ``potential`` < ``confirmed`` < ``blocking``. A confirmed
conflict becomes blocking when the two tasks depend on each other, because
then neither can be integrated without the other's fix.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import itertools
import logging
import os
import re
from dataclasses import dataclass, field

from cptr.models.coordination import CoordConflict, CoordTask, CoordTaskDep
from cptr.utils.coordination import changes as ch
from cptr.utils.git import GitError, _run
from cptr.utils.identity import ExecutionIdentity

logger = logging.getLogger(__name__)

LOCKFILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "go.sum",
    "Gemfile.lock",
    "composer.lock",
}
MANIFESTS = {"package.json", "pyproject.toml", "go.mod", "Cargo.toml", "Gemfile", "composer.json"}
SHARED_CONFIG_PATTERNS = [
    ".github/*",
    ".gitlab-ci.yml",
    "Dockerfile",
    "docker-compose*.yml",
    "*.config.js",
    "*.config.ts",
    "*.config.mjs",
    "tsconfig*.json",
    "setup.cfg",
    "tox.ini",
    ".env.example",
    "Makefile",
    "*.toml",
    "*.ini",
]
MIGRATION_DIR_RE = re.compile(r"(^|/)(migrations|alembic)(/versions)?/")

READY_STATUSES = {"integration_ready", "completed"}
SCANNED_STATUSES = {"queued", "running", "blocked", "paused", "reviewing", "integration_ready"}


@dataclass
class Finding:
    task_a: str
    task_b: str | None
    level: str
    kind: str
    evidence: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        pair = "|".join(sorted(t for t in (self.task_a, self.task_b) if t))
        subject = self.evidence.get("subject", "")
        return hashlib.sha1(f"{pair}\0{self.kind}\0{subject}".encode()).hexdigest()


def _is_shared_config(path: str) -> bool:
    name = os.path.basename(path)
    if name in MANIFESTS:
        return True
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in SHARED_CONFIG_PATTERNS)


def _tokens(title: str) -> set[str]:
    stop = {"the", "a", "an", "to", "of", "and", "for", "in", "on", "with", "add", "fix", "update"}
    return {t for t in re.findall(r"[a-z0-9]+", title.lower()) if t not in stop and len(t) > 1}


def similar_titles(a: str, b: str, threshold: float = 0.6) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


def classify_pair(
    a: CoordTask, b: CoordTask, ca: ch.ChangeSet | None, cb: ch.ChangeSet | None, related: bool
) -> list[Finding]:
    """Deterministic findings for one pair from change sets and metadata (no Git calls)."""
    findings: list[Finding] = []
    pair = (a.id, b.id)

    if similar_titles(a.title, b.title):
        findings.append(
            Finding(
                *pair,
                "info",
                "duplicate_request",
                {"subject": "title", "titles": [a.title, b.title]},
            )
        )
    declared = set(a.files or []) & set(b.files or [])

    if ca is None or cb is None:
        if declared:
            findings.append(
                Finding(
                    *pair,
                    "info",
                    "declared_overlap",
                    {"subject": "declared", "files": sorted(declared)},
                )
            )
        return findings

    overlap = ca.files & cb.files
    lock = sorted(f for f in overlap if os.path.basename(f) in LOCKFILES)
    config = sorted(f for f in overlap - set(lock) if _is_shared_config(f))
    plain = sorted(overlap - set(lock) - set(config))
    if lock:
        findings.append(
            Finding(*pair, "potential", "lockfile", {"subject": "lockfile", "files": lock})
        )
    if config:
        findings.append(
            Finding(*pair, "potential", "shared_config", {"subject": "config", "files": config})
        )
    if plain:
        findings.append(
            Finding(
                *pair,
                "potential",
                "file_overlap",
                {
                    "subject": "files",
                    "files": plain,
                    "uncommitted": sorted(
                        f for f in plain if f in ca.uncommitted or f in cb.uncommitted
                    ),
                },
            )
        )
    if declared - overlap:
        findings.append(
            Finding(
                *pair,
                "info",
                "declared_overlap",
                {"subject": "declared", "files": sorted(declared - overlap)},
            )
        )

    def new_migrations(cs: ch.ChangeSet) -> dict[str, list[str]]:
        dirs: dict[str, list[str]] = {}
        for path, status in {**cs.committed, **cs.uncommitted}.items():
            if status == "A" and MIGRATION_DIR_RE.search(path):
                dirs.setdefault(os.path.dirname(path), []).append(path)
        return dirs

    ma, mb = new_migrations(ca), new_migrations(cb)
    for directory in sorted(set(ma) & set(mb)):
        findings.append(
            Finding(
                *pair,
                "potential",
                "migration_heads",
                {"subject": directory, "files": sorted(ma[directory] + mb[directory])},
            )
        )

    for owner, user, cs_owner, cs_user in ((a, b, ca, cb), (b, a, cb, ca)):
        touched = {
            symbol: files
            for symbol, files in cs_owner.interface_changes.items()
            if symbol in cs_user.added_identifiers and symbol not in cs_user.interface_changes
        }
        if touched:
            findings.append(
                Finding(
                    owner.id,
                    user.id,
                    "potential",
                    "interface",
                    {
                        "subject": f"interface:{owner.id}",
                        "changed_by": owner.id,
                        "used_by": user.id,
                        "symbols": {k: sorted(v) for k, v in sorted(touched.items())},
                    },
                )
            )

    mismatches = []
    for path_a, specs_a in ca.dependency_specs.items():
        for path_b, specs_b in cb.dependency_specs.items():
            if os.path.basename(path_a) != os.path.basename(path_b):
                continue
            for name in sorted(set(specs_a) & set(specs_b)):
                if specs_a[name] != specs_b[name]:
                    mismatches.append(
                        {
                            "package": name,
                            "manifest": path_a,
                            a.id: specs_a[name],
                            b.id: specs_b[name],
                        }
                    )
    if mismatches:
        findings.append(
            Finding(
                *pair,
                "potential",
                "dependency_version",
                {"subject": "deps", "packages": mismatches},
            )
        )
    return findings


_merge_cache: dict[tuple[str, str], list[str] | None] = {}


async def merge_conflicts(
    repo_root: str, left: str, right: str, identity: ExecutionIdentity | None = None
) -> list[str] | None:
    """Files that conflict when merging two commits; ``[]`` if clean, ``None`` if unknown."""
    key = (left, right) if left < right else (right, left)
    if key in _merge_cache:
        return _merge_cache[key]
    try:
        code, out, _ = await _run(
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            left,
            right,
            cwd=repo_root,
            check=False,
            identity=identity,
        )
    except GitError:
        return None
    if code == 0:
        result: list[str] | None = []
    elif code == 1:
        result = sorted({line for line in out.splitlines()[1:] if line.strip()})
    else:  # e.g. git < 2.38 or unrelated histories
        result = None
    if len(_merge_cache) > 512:
        _merge_cache.clear()
    _merge_cache[key] = result
    return result


async def base_branch_head(task: CoordTask, identity: ExecutionIdentity | None) -> str | None:
    branch = (task.meta or {}).get("base_branch")
    if not branch:
        return None
    code, out, _ = await _run(
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}^{{commit}}",
        cwd=task.repo_root,
        check=False,
        identity=identity,
    )
    return out.strip() if code == 0 and out.strip() else None


async def detect(repo_key: str, identity: ExecutionIdentity | None = None) -> list[Finding]:
    """All current findings for a repository."""
    tasks = await CoordTask.query(repo_key=repo_key, statuses=SCANNED_STATUSES)
    if not tasks:
        return []
    by_id = {t.id: t for t in tasks}
    all_tasks = {t.id: t for t in await CoordTask.query(repo_key=repo_key)}
    edges = await CoordTaskDep.all_for_repo(repo_key)
    related = {frozenset(e) for e in edges}
    with_worktree = [t for t in tasks if t.worktree_path]
    computed = await asyncio.gather(*(ch.compute(t, identity) for t in with_worktree))
    sets = {t.id: cs for t, cs in zip(with_worktree, computed)}

    findings: list[Finding] = []
    writers = [t for t in tasks if t.worktree_path]
    for a, b in itertools.combinations(writers, 2):
        ca, cb = sets.get(a.id), sets.get(b.id)
        is_related = frozenset((a.id, b.id)) in related
        pair_findings = classify_pair(a, b, ca, cb, is_related)
        findings.extend(pair_findings)
        overlap = (ca.files & cb.files) if ca and cb else set()
        if overlap and ca.has_commits and cb.has_commits:
            files = await merge_conflicts(a.repo_root, ca.head, cb.head, identity)
            if files:
                level = "blocking" if is_related else "confirmed"
                findings.append(
                    Finding(
                        a.id,
                        b.id,
                        level,
                        "merge_conflict",
                        {
                            "subject": "merge",
                            "files": files,
                            "heads": {a.id: ca.head, b.id: cb.head},
                            "dependent": is_related,
                        },
                    )
                )

    for task in writers:
        cs = sets.get(task.id)
        if not cs or not cs.has_commits:
            continue
        base_head = await base_branch_head(task, identity)
        if base_head and base_head != cs.merge_base:
            files = await merge_conflicts(task.repo_root, base_head, cs.head, identity)
            if files:
                findings.append(
                    Finding(
                        task.id,
                        None,
                        "confirmed",
                        "base_divergence",
                        {
                            "subject": "base",
                            "files": files,
                            "base_branch": task.meta.get("base_branch"),
                            "base_head": base_head,
                        },
                    )
                )

    for task_id, dep_id in edges:
        task, dep = by_id.get(task_id), all_tasks.get(dep_id)
        if not task or not dep:
            continue
        if dep.status == "failed":
            findings.append(
                Finding(
                    task_id,
                    dep_id,
                    "blocking",
                    "dependency_failed",
                    {"subject": f"dep:{dep_id}", "dependency": dep_id},
                )
            )
        elif dep.status not in READY_STATUSES and task.status in {
            "running",
            "reviewing",
            "integration_ready",
        }:
            findings.append(
                Finding(
                    task_id,
                    dep_id,
                    "info",
                    "unfinished_dependency",
                    {
                        "subject": f"dep:{dep_id}",
                        "dependency": dep_id,
                        "dependency_status": dep.status,
                    },
                )
            )
    return findings


async def scan(
    repo_key: str, identity: ExecutionIdentity | None = None
) -> tuple[list[tuple[CoordConflict, str]], list[CoordConflict]]:
    """Detect, persist and auto-resolve. Returns ``(upserted [(row, change)], resolved)``."""
    findings = await detect(repo_key, identity)
    upserted = []
    seen = set()
    for f in findings:
        row, change = await CoordConflict.upsert(
            repo_key=repo_key,
            task_a=f.task_a,
            task_b=f.task_b,
            level=f.level,
            kind=f.kind,
            evidence=f.evidence,
            fingerprint=f.fingerprint,
        )
        seen.add(row.id)
        upserted.append((row, change))
    resolved = []
    for row in await CoordConflict.query(repo_key, statuses={"open", "acknowledged"}):
        if row.id not in seen and row.kind != "dependency_cycle":
            await CoordConflict.set_status(row.id, "resolved")
            row.status = "resolved"
            resolved.append(row)
    return upserted, resolved
