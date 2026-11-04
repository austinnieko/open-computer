"""Deterministic per-task change sets, read from Git (no LLM involvement).

A task's change set is everything its worktree differs from the task's base:
committed (``merge-base..HEAD``) plus staged, unstaged and untracked files.
Results are cached per task by ``(HEAD, status)`` so unchanged worktrees are
never re-diffed.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

from cptr.models.coordination import CoordTask
from cptr.utils.git import GitError, _run
from cptr.utils.identity import ExecutionIdentity

# Public-interface heuristics: top-level definitions whose removal or change can
# break other code. Private names (leading underscore) are ignored.
_SIGNATURE_RES = [
    re.compile(r"^(?:async\s+)?def\s+([A-Za-z]\w*)\s*\("),  # python
    re.compile(r"^class\s+([A-Za-z]\w*)\b"),  # python / js / ts
    re.compile(
        r"^export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var|type|interface|enum)"
        r"\s+([A-Za-z_$][\w$]*)"
    ),
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Z]\w*)\s*\("),  # go (exported = capitalised)
    re.compile(r"^pub\s+(?:async\s+)?(?:fn|struct|enum|trait|type)\s+([A-Za-z]\w*)"),  # rust
]
_CODE_EXTS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".svelte", ".go", ".rs"}
_MAX_INTERFACE_FILES = 40


@dataclass
class ChangeSet:
    task_id: str
    head: str
    merge_base: str
    committed: dict[str, str] = field(default_factory=dict)  # path -> status letter
    uncommitted: dict[str, str] = field(default_factory=dict)
    # symbol -> files where its definition was removed or changed
    interface_changes: dict[str, list[str]] = field(default_factory=dict)
    # identifiers referenced on added lines (to match against others' interface changes)
    added_identifiers: set[str] = field(default_factory=set)
    # manifest file -> {package: spec} added or changed by this task
    dependency_specs: dict[str, dict[str, str]] = field(default_factory=dict)
    cache_key: str = ""

    @property
    def files(self) -> set[str]:
        return set(self.committed) | set(self.uncommitted)

    @property
    def has_commits(self) -> bool:
        return bool(self.committed)


_cache: dict[str, ChangeSet] = {}


def clear_cache(task_id: str | None = None) -> None:
    if task_id is None:
        _cache.clear()
    else:
        _cache.pop(task_id, None)


def _parse_name_status(out: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        if status in {"R", "C"} and len(parts) >= 3:
            files[parts[1]] = "D" if status == "R" else "A"  # old path vacated
            files[parts[2]] = "A"
        else:
            files[parts[1]] = status
    return files


def _parse_porcelain(out: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for line in out.splitlines():
        if len(line) < 4 or line.startswith("## "):
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:
            old, path = path.split(" -> ", 1)
            files[old] = "D"
        if code == "??":
            files[path] = "A"
        else:
            files[path] = (code.strip() or "M")[:1]
    return files


def _signature_names(lines: list[str]) -> set[str]:
    names = set()
    for line in lines:
        for regex in _SIGNATURE_RES:
            match = regex.match(line)
            if match and not match.group(1).startswith("_"):
                names.add(match.group(1))
                break
    return names


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_PKG_JSON_DEP_RE = re.compile(r'^\s*"(@?[\w./-]+)"\s*:\s*"([^"]+)"\s*,?\s*$')
_REQ_RE = re.compile(
    r'^\s*"?([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([<>=!~^][^";,]*)"?,?\s*$'
)
_MANIFESTS = {"package.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml"}


def _parse_dependency_lines(path: str, added: list[str]) -> dict[str, str]:
    name = os.path.basename(path)
    specs: dict[str, str] = {}
    for line in added:
        if name == "package.json":
            match = _PKG_JSON_DEP_RE.match(line)
            if match and re.match(r"^[\^~<>=*\dvwl]|^workspace:|^npm:", match.group(2)):
                specs[match.group(1)] = match.group(2)
        elif name in {"pyproject.toml", "requirements.txt"} or name.startswith("requirements"):
            match = _REQ_RE.match(line)
            if match:
                specs[match.group(1).lower().replace("_", "-")] = match.group(3).strip()
        elif name == "go.mod":
            parts = line.split()
            if len(parts) >= 2 and "/" in parts[0] and parts[1].startswith("v"):
                specs[parts[0]] = parts[1]
        elif name == "Cargo.toml":
            match = re.match(r'^\s*([\w-]+)\s*=\s*"([^"]+)"', line)
            if match:
                specs[match.group(1)] = match.group(2)
    return specs


async def _diff_details(
    cwd: str, merge_base: str, files: set[str], identity: ExecutionIdentity | None
) -> tuple[dict[str, list[str]], set[str], dict[str, dict[str, str]]]:
    """One ``git diff`` over the relevant files: interface, identifier and dependency evidence."""
    code_files = [f for f in files if os.path.splitext(f)[1] in _CODE_EXTS][:_MAX_INTERFACE_FILES]
    manifest_files = [
        f
        for f in files
        if os.path.basename(f) in _MANIFESTS or os.path.basename(f).startswith("requirements")
    ]
    targets = code_files + manifest_files
    if not targets:
        return {}, set(), {}
    try:
        _, out, _ = await _run(
            "diff",
            "-U0",
            "--no-color",
            "--no-ext-diff",
            merge_base,
            "--",
            *targets,
            cwd=cwd,
            check=False,
            identity=identity,
        )
    except GitError:
        return {}, set(), {}

    removed: dict[str, list[str]] = {}
    added: dict[str, list[str]] = {}
    current = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("--- "):
            continue
        elif current and line.startswith("-"):
            removed.setdefault(current, []).append(line[1:])
        elif current and line.startswith("+"):
            added.setdefault(current, []).append(line[1:])

    interface: dict[str, list[str]] = {}
    identifiers: set[str] = set()
    deps: dict[str, dict[str, str]] = {}
    for path in set(removed) | set(added):
        if path in manifest_files:
            specs = _parse_dependency_lines(path, added.get(path, []))
            if specs:
                deps[path] = specs
            continue
        for name in _signature_names(removed.get(path, [])):
            interface.setdefault(name, []).append(path)
        for line in added.get(path, []):
            identifiers.update(_IDENT_RE.findall(line))
    return interface, identifiers, deps


async def compute(task: CoordTask, identity: ExecutionIdentity | None = None) -> ChangeSet | None:
    """Return the task's change set, or ``None`` if it has no usable worktree."""
    cwd = task.worktree_path
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        _, head, _ = await _run("rev-parse", "HEAD", cwd=cwd, identity=identity)
        _, status, _ = await _run(
            "status", "--porcelain=v1", "--untracked-files=all", cwd=cwd, identity=identity
        )
    except GitError:
        return None
    head = head.strip()
    key = hashlib.sha1(f"{head}\0{task.base_sha}\0{status}".encode()).hexdigest()
    cached = _cache.get(task.id)
    if cached and cached.cache_key == key:
        return cached

    merge_base = head
    if task.base_sha:
        code, mb, _ = await _run(
            "merge-base", task.base_sha, head, cwd=cwd, check=False, identity=identity
        )
        merge_base = mb.strip() if code == 0 and mb.strip() else task.base_sha
    committed: dict[str, str] = {}
    if merge_base != head:
        _, out, _ = await _run(
            "diff",
            "--name-status",
            "-M",
            "--no-color",
            merge_base,
            head,
            cwd=cwd,
            check=False,
            identity=identity,
        )
        committed = _parse_name_status(out)
    uncommitted = _parse_porcelain(status)
    interface, identifiers, deps = await _diff_details(
        cwd, merge_base, set(committed) | set(uncommitted), identity
    )
    result = ChangeSet(
        task_id=task.id,
        head=head,
        merge_base=merge_base,
        committed=committed,
        uncommitted=uncommitted,
        interface_changes=interface,
        added_identifiers=identifiers,
        dependency_specs=deps,
        cache_key=key,
    )
    _cache[task.id] = result
    return result
