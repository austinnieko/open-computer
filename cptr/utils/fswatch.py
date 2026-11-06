"""Pruned recursive file watching for Linux (inotify).

Why not watchdog's recursive inotify observer: it adds a kernel watch for
every directory below the root, including ``.git``, ``node_modules`` and
``.venv``, and only filters their events afterwards. An agent running
``npm install``, a build or a git checkout floods the kernel queue; watchdog 7
then answers each overflow by dropping every watch and re-walking the whole
tree on its reader thread, which overflows the queue again ("Inotify queue
overflowed; reconciling recursive watches", several times a second).

This watcher instead:

* never watches high-churn or git-ignored directories (no events are queued
  for them at all), while still watching every other source directory,
  including untracked ones;
* listens for whole-file writes (``IN_CLOSE_WRITE``) rather than every
  ``write()``, which is what the file browser and git badges need;
* shares one inotify instance and one reader thread across all roots, so
  overlapping roots (a workspace and a folder inside it, several tabs on one
  worktree) share kernel watches instead of duplicating them;
* keeps the reader thread cheap: new directories are walked and overflows
  are resynced on a separate worker, coalesced and rate limited, so an event
  storm produces at most one resync per root every few seconds;
* bounds what it buffers: past a cap, a root's pending changes collapse to
  "the root changed", which makes the UI refresh once instead of growing a
  queue.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import logging
import os
import select
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from cptr.utils.gitignore import is_gitignored, load_gitignore

logger = logging.getLogger(__name__)

# Directories never watched (by name, at any depth). Their contents churn
# during installs, builds and git operations and never matter for the file
# browser's auto-refresh or git badges (git itself reports status).
# Build outputs (dist/, build/, target/, coverage/) are skipped when the
# repository's .gitignore ignores them, so a source folder that happens to
# be called "build" is still watched.
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".cache",
        ".parcel-cache",
        ".turbo",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".vite",
        ".gradle",
        ".terraform",
        ".direnv",
        ".DS_Store",
    }
)

MAX_PENDING_PER_ROOT = 2_000
"""Past this many distinct changed paths, report just the root."""
MAX_WATCHES = 200_000
"""Hard cap on kernel watches for the whole process."""
RESYNC_MIN_INTERVAL = 5.0
"""Minimum seconds between overflow resyncs of one root."""
OVERFLOW_LOG_INTERVAL = 60.0

# inotify(7)
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_DONT_FOLLOW = 0x02000000
IN_EXCL_UNLINK = 0x04000000
IN_ISDIR = 0x40000000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000

WATCH_MASK = (
    IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
    | IN_ONLYDIR
    | IN_DONT_FOLLOW
    | IN_EXCL_UNLINK
)

_EVENT_HEADER = struct.Struct("iIII")


def is_supported() -> bool:
    return hasattr(os, "uname") and os.uname().sysname == "Linux" and _libc() is not None


_LIBC = None


def _libc():
    global _LIBC
    if _LIBC is None:
        try:
            lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            lib.inotify_init1.argtypes = [ctypes.c_int]
            lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            lib.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
            _LIBC = lib
        except (OSError, AttributeError):
            _LIBC = False
    return _LIBC or None


def parse_events(buf: bytes) -> list[tuple[int, int, int, str]]:
    """Decode a read() of an inotify fd into (wd, mask, cookie, name)."""
    events = []
    offset = 0
    while offset + _EVENT_HEADER.size <= len(buf):
        wd, mask, cookie, length = _EVENT_HEADER.unpack_from(buf, offset)
        offset += _EVENT_HEADER.size
        name = buf[offset : offset + length].rstrip(b"\0").decode("utf-8", "surrogateescape")
        offset += length
        events.append((wd, mask, cookie, name))
    return events


@dataclass
class _Root:
    path: str
    refs: int = 0
    pending: set[str] = field(default_factory=set)
    saturated: bool = False
    ignore_base: Path | None = None
    ignore_patterns: tuple = ()
    needs_resync: bool = False
    last_resync: float = 0.0
    overflows: int = 0
    last_overflow_log: float = 0.0


class InotifyWatcher:
    """One inotify instance shared by every watched root in the process."""

    def __init__(self, *, start_threads: bool = True) -> None:
        lib = _libc()
        if lib is None:
            raise OSError("inotify is not available")
        self._lib = lib
        fd = lib.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        self._fd = fd
        self._lock = threading.Lock()
        self._roots: dict[str, _Root] = {}
        self._wd_path: dict[int, str] = {}
        self._path_wd: dict[str, int] = {}
        self._new_dirs: set[str] = set()
        self._work = threading.Event()
        self._closed = False
        self._watch_limit_logged = False
        self._wake_r, self._wake_w = os.pipe()
        self._reader = threading.Thread(target=self._read_loop, name="cptr-inotify", daemon=True)
        self._worker = threading.Thread(target=self._work_loop, name="cptr-inotify-sync", daemon=True)
        if start_threads:
            self._reader.start()
            self._worker.start()

    # ── Public API ────────────────────────────────────────────────

    def add_root(self, path: str) -> None:
        """Start (or share) watching ``path``; balanced by :meth:`remove_root`."""
        with self._lock:
            root = self._roots.get(path)
            if root is not None:
                root.refs += 1
                return
            root = _Root(path=path, refs=1)
            try:
                root.ignore_base, root.ignore_patterns = load_gitignore(Path(path))
            except OSError:
                pass
            self._roots[path] = root
        # Initial walk off the caller's lock; root watch failures surface.
        self._watch_tree(path, root, raise_root=True)

    def remove_root(self, path: str) -> None:
        with self._lock:
            root = self._roots.get(path)
            if root is None:
                return
            root.refs -= 1
            if root.refs > 0:
                return
            del self._roots[path]
            remaining = list(self._roots)
            stale = [
                (p, wd)
                for p, wd in self._path_wd.items()
                if not any(_within(p, r) for r in remaining)
            ]
            for p, wd in stale:
                self._path_wd.pop(p, None)
                self._wd_path.pop(wd, None)
                self._lib.inotify_rm_watch(self._fd, wd)

    def drain(self, path: str) -> list[str]:
        """Changed paths under ``path`` since the last drain."""
        with self._lock:
            root = self._roots.get(path)
            if root is None or not root.pending:
                return []
            paths = [root.path] if root.saturated else list(root.pending)
            root.pending.clear()
            root.saturated = False
            return paths

    @property
    def watch_count(self) -> int:
        with self._lock:
            return len(self._path_wd)

    def close(self) -> None:
        self._closed = True
        self._work.set()
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        for thread in (self._reader, self._worker):
            if thread.is_alive():
                thread.join(timeout=2)
        for fd in (self._fd, self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass

    # ── Watch management ──────────────────────────────────────────

    def _excluded(self, dir_path: str) -> bool:
        if os.path.basename(dir_path) in EXCLUDED_DIR_NAMES:
            return True
        for root in self._roots.values():
            if _within(dir_path, root.path) and root.ignore_patterns:
                try:
                    if is_gitignored(
                        Path(dir_path), root.ignore_base, root.ignore_patterns, is_dir=True
                    ):
                        return True
                except (ValueError, OSError):
                    pass
        return False

    def _add_watch(self, path: str) -> bool:
        if path in self._path_wd:
            return True
        if len(self._path_wd) >= MAX_WATCHES:
            self._log_watch_limit(f"{MAX_WATCHES} watched directories")
            return False
        wd = self._lib.inotify_add_watch(self._fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            err = ctypes.get_errno()
            if err == errno.ENOSPC:
                self._log_watch_limit("the system's fs.inotify.max_user_watches")
            elif err not in (errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.ELOOP):
                logger.debug("inotify_add_watch(%s) failed: %s", path, os.strerror(err))
            if err in (errno.ENOENT, errno.ENOTDIR, errno.EACCES):
                raise FileNotFoundError(err, os.strerror(err), path)
            return False
        old = self._wd_path.get(wd)
        if old is not None and old != path:
            self._path_wd.pop(old, None)
        self._wd_path[wd] = path
        self._path_wd[path] = wd
        return True

    def _log_watch_limit(self, what: str) -> None:
        if not self._watch_limit_logged:
            self._watch_limit_logged = True
            logger.warning(
                "File watching hit %s; some folders will not auto-refresh", what
            )

    def _watch_tree(self, top: str, root: _Root | None, *, raise_root: bool = False) -> list[str]:
        """Watch ``top`` and its non-excluded subdirectories; return new dirs."""
        added: list[str] = []
        stack = [top]
        first = True
        while stack and not self._closed:
            current = stack.pop()
            with self._lock:
                if not first and self._excluded(current):
                    continue
                try:
                    known = current in self._path_wd
                    if not self._add_watch(current):
                        continue
                    if not known:
                        added.append(current)
                except FileNotFoundError:
                    if first and raise_root:
                        raise
                    continue
            first = False
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                        except OSError:
                            pass
            except OSError:
                pass
        return added

    # ── Event handling (reader thread) ────────────────────────────

    def _record(self, path: str) -> None:
        """Mark ``path`` (and its folder) changed for every root containing it."""
        parent = os.path.dirname(path)
        for root in self._roots.values():
            if not _within(path, root.path) or root.saturated:
                continue
            root.pending.add(path)
            if parent and _within(parent, root.path):
                root.pending.add(parent)
            if len(root.pending) > MAX_PENDING_PER_ROOT:
                root.saturated = True
                root.pending = {root.path}

    def handle_events(self, events: list[tuple[int, int, int, str]]) -> None:
        overflow = False
        with self._lock:
            for wd, mask, _cookie, name in events:
                if mask & IN_Q_OVERFLOW:
                    overflow = True
                    continue
                if mask & IN_IGNORED:
                    path = self._wd_path.pop(wd, None)
                    if path is not None and self._path_wd.get(path) == wd:
                        del self._path_wd[path]
                    continue
                base = self._wd_path.get(wd)
                if base is None:
                    continue
                path = os.path.join(base, name) if name else base
                is_dir = bool(mask & IN_ISDIR)
                if name and name in EXCLUDED_DIR_NAMES:
                    # The folder itself appearing/disappearing is visible in
                    # the listing; its contents are never watched.
                    if is_dir and mask & (IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO):
                        self._record(path)
                    continue
                if is_dir and mask & IN_MOVED_FROM:
                    # Watches below a moved-away folder now point at the new
                    # location (or outside every root); forget them, the
                    # matching MOVED_TO re-adds them under the right path.
                    self._forget_tree(path)
                if is_dir and mask & (IN_CREATE | IN_MOVED_TO):
                    self._new_dirs.add(path)
                    self._work.set()
                if mask & (IN_DELETE_SELF | IN_MOVE_SELF) and not name:
                    if mask & IN_MOVE_SELF:
                        self._forget_tree(path)
                    for root in self._roots.values():
                        if root.path == path:
                            root.needs_resync = True
                            self._work.set()
                self._record(path)
            if overflow:
                for root in self._roots.values():
                    root.needs_resync = True
                    root.overflows += 1
                self._work.set()

    def _forget_tree(self, path: str) -> None:
        prefix = path + os.sep
        for p in [p for p in self._path_wd if p == path or p.startswith(prefix)]:
            wd = self._path_wd.pop(p)
            if self._wd_path.get(wd) == p:
                del self._wd_path[wd]
            self._lib.inotify_rm_watch(self._fd, wd)

    def _read_loop(self) -> None:
        while not self._closed:
            try:
                ready, _, _ = select.select([self._fd, self._wake_r], [], [], 1.0)
            except (OSError, ValueError):
                return
            if self._fd not in ready:
                continue
            batch: list[tuple[int, int, int, str]] = []
            while True:
                try:
                    buf = os.read(self._fd, 256 * 1024)
                except BlockingIOError:
                    break
                except OSError:
                    return
                if not buf:
                    break
                batch.extend(parse_events(buf))
            if batch:
                try:
                    self.handle_events(batch)
                except Exception:
                    logger.exception("inotify event handling failed")

    # ── Background work (worker thread) ───────────────────────────

    def _work_loop(self) -> None:
        # The timeout also retries resyncs the rate limit postponed.
        while not self._closed:
            self._work.wait(timeout=RESYNC_MIN_INTERVAL)
            self._work.clear()
            if self._closed:
                return
            try:
                self.run_pending_work()
            except Exception:
                logger.exception("inotify background sync failed")

    def run_pending_work(self) -> None:
        """Watch newly created folders and resync overflowed roots (coalesced)."""
        with self._lock:
            new_dirs = sorted(self._new_dirs)
            self._new_dirs.clear()
        # Parents first; a child already covered by its parent's walk is cheap.
        for d in new_dirs:
            for added in self._watch_tree(d, None):
                if added == d:
                    continue
                # Files created before the folder's watch existed would be
                # missed; report the new folders so listings refresh.
                with self._lock:
                    self._record(added)
            # Files directly inside, created before the watch was added.
            try:
                with os.scandir(d) as it:
                    names = [e.path for e in it]
            except OSError:
                names = []
            with self._lock:
                for p in names[: MAX_PENDING_PER_ROOT]:
                    self._record(p)

        now = time.monotonic()
        with self._lock:
            due = [
                r
                for r in self._roots.values()
                if r.needs_resync and now - r.last_resync >= RESYNC_MIN_INTERVAL
            ]
            for r in due:
                r.needs_resync = False
                r.last_resync = now
        for root in due:
            # Deletions may have been among the lost events: forget watches
            # on folders that are gone, then pick up folders that appeared.
            with self._lock:
                gone = [p for p in self._path_wd if _within(p, root.path)]
            gone = [p for p in gone if not os.path.isdir(p)]
            with self._lock:
                for p in gone:
                    wd = self._path_wd.pop(p, None)
                    if wd is not None and self._wd_path.get(wd) == p:
                        del self._wd_path[wd]
            added = self._watch_tree(root.path, root)
            with self._lock:
                # Events were lost: tell subscribers the whole root may differ.
                root.saturated = True
                root.pending = {root.path}
                if now - root.last_overflow_log >= OVERFLOW_LOG_INTERVAL:
                    logger.warning(
                        "File events were dropped under %s (%d overflow(s) since last report); "
                        "resynced, %d new folder(s) watched",
                        root.path,
                        root.overflows,
                        len(added),
                    )
                    root.last_overflow_log = now
                    root.overflows = 0


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


_shared: InotifyWatcher | None = None
_shared_lock = threading.Lock()


def shared_watcher() -> InotifyWatcher:
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = InotifyWatcher()
        return _shared
