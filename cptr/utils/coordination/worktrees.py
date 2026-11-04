"""Task worktrees: identity, path validation, create/reuse, state, safe removal.

Built on ``cptr.utils.git`` (``worktrees``, ``create_worktree``, ``_run``).
Task worktrees live under a managed root beside the repository::

    <repo-parent>/.cptr-worktrees/<repo-name>/<task-slug>

Only paths that resolve inside that root are accepted for task worktrees, so a
task can never be pointed at an unrelated directory.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from cptr.utils.git import GitError, _run, create_worktree, worktrees
from cptr.utils.identity import ExecutionIdentity

MANAGED_DIRNAME = ".cptr-worktrees"
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


class WorktreeError(Exception):
    """A worktree operation was refused or failed."""


@dataclass(frozen=True)
class RepoIdentity:
    key: str  # realpath of the shared git dir; identical for every worktree
    main_root: str  # the primary checkout


async def repo_identity(path: str, identity: ExecutionIdentity | None = None) -> RepoIdentity:
    """Resolve the repository a path belongs to, independent of which worktree it is."""
    if not os.path.isdir(path):
        raise WorktreeError(f"Not a directory: {path}")
    try:
        _, out, _ = await _run(
            "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path, identity=identity
        )
    except GitError as exc:
        raise WorktreeError(f"Not a git repository: {path}") from exc
    common = os.path.realpath(out.strip())
    if os.path.basename(common) == ".git":
        main_root = os.path.dirname(common)
    else:  # bare repository or unusual layout: fall back to the toplevel of `path`
        _, top, _ = await _run("rev-parse", "--show-toplevel", cwd=path, identity=identity)
        main_root = os.path.realpath(top.strip())
    return RepoIdentity(key=common, main_root=main_root)


def managed_root(main_root: str) -> str:
    main_root = os.path.realpath(main_root)
    return os.path.join(os.path.dirname(main_root), MANAGED_DIRNAME, os.path.basename(main_root))


def slugify(text: str, *, max_len: int = 40) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip(".-")
    return (slug[:max_len].rstrip(".-")) or "task"


def default_worktree_path(main_root: str, slug: str) -> str:
    return os.path.join(managed_root(main_root), slugify(slug))


def is_within(path: str, root: str) -> bool:
    """True if ``path`` resolves (following symlinks) strictly inside ``root``."""
    real = os.path.realpath(path)
    real_root = os.path.realpath(root)
    return real != real_root and os.path.commonpath([real, real_root]) == real_root


def validate_task_path(main_root: str, path: str) -> str:
    """Return the normalised absolute path, or raise if it is outside the managed root."""
    if not os.path.isabs(path):
        raise WorktreeError("Worktree path must be absolute")
    if ".." in path.replace("\\", "/").split("/"):
        raise WorktreeError("Worktree path must not contain '..'")
    root = managed_root(main_root)
    if not is_within(path, root):
        raise WorktreeError(f"Worktree path must be inside {root}")
    return os.path.realpath(path)


async def validate_branch_name(
    repo_root: str, branch: str, identity: ExecutionIdentity | None = None
) -> str:
    branch = branch.strip()
    if not branch or branch.startswith("-") or branch in {"HEAD", "main", "master"}:
        raise WorktreeError(f"Invalid task branch name: {branch!r}")
    code, out, _ = await _run(
        "check-ref-format", "--branch", branch, cwd=repo_root, check=False, identity=identity
    )
    if code != 0:
        raise WorktreeError(f"Invalid branch name: {branch!r}")
    return out.strip() or branch


async def branch_exists(
    repo_root: str, branch: str, identity: ExecutionIdentity | None = None
) -> bool:
    code, _, _ = await _run(
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        cwd=repo_root,
        check=False,
        identity=identity,
    )
    return code == 0


async def resolve_sha(
    repo_root: str, ref: str, identity: ExecutionIdentity | None = None
) -> str | None:
    code, out, _ = await _run(
        "rev-parse",
        "--verify",
        "--quiet",
        f"{ref}^{{commit}}",
        cwd=repo_root,
        check=False,
        identity=identity,
    )
    return out.strip() if code == 0 and out.strip() else None


async def list_worktrees(
    repo_root: str, identity: ExecutionIdentity | None = None
) -> list[dict[str, Any]]:
    return (await worktrees(repo_root, identity))["worktrees"]


async def is_worktree_of(
    path: str, repo: RepoIdentity, identity: ExecutionIdentity | None = None
) -> bool:
    try:
        other = await repo_identity(path, identity)
    except (WorktreeError, GitError):
        return False
    return other.key == repo.key


async def ensure_worktree(
    repo: RepoIdentity,
    *,
    branch: str,
    path: str,
    base_ref: str | None = None,
    identity: ExecutionIdentity | None = None,
) -> dict[str, Any]:
    """Create the task worktree, or reuse an existing one for the same branch.

    Returns ``{"path", "branch", "reused", "created_branch"}``. Never moves,
    resets or deletes anything that already exists.
    """
    path = validate_task_path(repo.main_root, path)
    branch = await validate_branch_name(repo.main_root, branch, identity)

    for item in await list_worktrees(repo.main_root, identity):
        if item.get("branch") != branch:
            continue
        existing = os.path.realpath(item["path"])
        if existing == path or is_within(existing, managed_root(repo.main_root)):
            return {"path": existing, "branch": branch, "reused": True, "created_branch": False}
        raise WorktreeError(
            f"Branch {branch!r} is already checked out at {existing}; refusing to reuse a "
            "checkout outside the managed worktree root"
        )

    if os.path.exists(path):
        if os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)  # empty leftover from a failed create; safe to replace
        else:
            raise WorktreeError(
                f"{path} exists and is not a worktree for branch {branch!r}; leaving it untouched"
            )

    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = await branch_exists(repo.main_root, branch, identity)
    try:
        await create_worktree(
            repo.main_root,
            branch,
            path,
            identity,
            base_ref=None if exists else base_ref,
            new_branch=not exists,
        )
    except GitError as exc:
        raise WorktreeError(f"git worktree add failed: {exc}") from exc
    return {"path": path, "branch": branch, "reused": False, "created_branch": not exists}


async def worktree_state(path: str, identity: ExecutionIdentity | None = None) -> dict[str, Any]:
    """Cheap, deterministic snapshot of a worktree. Missing paths are reported, not raised."""
    if not os.path.isdir(path):
        return {"exists": False}
    try:
        _, status, _ = await _run(
            "status",
            "--porcelain=v1",
            "-b",
            "--untracked-files=normal",
            cwd=path,
            identity=identity,
        )
        _, head, _ = await _run("rev-parse", "HEAD", cwd=path, check=False, identity=identity)
    except GitError as exc:
        return {"exists": True, "error": str(exc)}
    lines = status.splitlines()
    header = lines[0] if lines and lines[0].startswith("## ") else ""
    changes = [line for line in lines if not line.startswith("## ")]
    branch = header[3:].split("...", 1)[0].strip() if header else ""
    return {
        "exists": True,
        "branch": "" if branch.startswith("HEAD") else branch,
        "head": head.strip(),
        "dirty": bool(changes),
        "changed_files": [line[3:] for line in changes],
    }


async def remove_worktree(
    repo: RepoIdentity,
    path: str,
    *,
    user_confirmed: bool,
    identity: ExecutionIdentity | None = None,
) -> None:
    """Remove a task worktree. The branch and its commits are kept.

    Requires explicit user confirmation and refuses dirty worktrees outright;
    there is deliberately no force option.
    """
    if not user_confirmed:
        raise WorktreeError("Removing a worktree requires explicit user approval")
    path = validate_task_path(repo.main_root, path)
    state = await worktree_state(path, identity)
    if not state.get("exists"):
        await _run("worktree", "prune", cwd=repo.main_root, identity=identity)
        return
    if state.get("dirty") or state.get("error"):
        raise WorktreeError(f"{path} has uncommitted changes; refusing to remove it")
    try:
        await _run("worktree", "remove", path, cwd=repo.main_root, identity=identity)
    except GitError as exc:
        raise WorktreeError(f"git worktree remove failed: {exc}") from exc
