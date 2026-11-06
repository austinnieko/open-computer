"""Pruned inotify watcher, driven through a fake kernel interface.

Run: uv run python -m unittest tests.test_fswatch -v

macOS has no inotify, so these tests replace libc's three inotify calls with
a fake that hands out watch descriptors like the kernel does (same path,
same wd). They check which folders get watched and how events, overflows
and new folders are handled; they do not exercise a real kernel queue.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cptr.utils import fswatch
from cptr.utils.fswatch import (
    IN_CLOSE_WRITE,
    IN_CREATE,
    IN_IGNORED,
    IN_ISDIR,
    IN_MOVED_FROM,
    IN_MOVED_TO,
    IN_Q_OVERFLOW,
)


class FakeInotify:
    def __init__(self) -> None:
        self.by_path: dict[bytes, int] = {}
        self.removed: list[int] = []
        self.next_wd = 1
        self.add_calls = 0

    def inotify_init1(self, _flags):
        r, _w = os.pipe()
        self._keep = _w
        return r

    def inotify_add_watch(self, _fd, path, _mask):
        self.add_calls += 1
        if not os.path.isdir(path):
            import ctypes

            ctypes.set_errno(2)
            return -1
        if path not in self.by_path:
            self.by_path[path] = self.next_wd
            self.next_wd += 1
        return self.by_path[path]

    def inotify_rm_watch(self, _fd, wd):
        self.removed.append(wd)
        for p, w in list(self.by_path.items()):
            if w == wd:
                del self.by_path[p]
        return 0


class WatcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.fake = FakeInotify()
        patcher = mock.patch.object(fswatch, "_libc", return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.w = fswatch.InotifyWatcher(start_threads=False)
        self.addCleanup(self.w.close)
        self.addCleanup(self.tmp.cleanup)

    def mk(self, *rel: str) -> str:
        p = os.path.join(self.root, *rel)
        os.makedirs(p, exist_ok=True)
        return p

    def watched(self) -> set[str]:
        return {os.path.relpath(p, self.root) for p in self.w._path_wd}

    def wd(self, *rel: str) -> int:
        return self.w._path_wd[os.path.join(self.root, *rel) if rel else self.root]


class PruningTests(WatcherTestCase):
    def test_high_churn_and_gitignored_folders_are_never_watched(self):
        self.mk("src", "lib")
        self.mk(".git", "objects", "ab")
        self.mk("node_modules", "react", "cjs")
        self.mk(".venv", "lib")
        self.mk("web", "node_modules", "x")
        self.mk("dist", "assets")
        self.mk("build")  # not ignored here: a real source folder
        self.mk(".omo")  # untracked agent state, not ignored: watched
        Path(self.root, ".gitignore").write_text("dist/\n")
        self.w.add_root(self.root)
        self.assertEqual(
            self.watched(), {".", "src", "src/lib", "web", "build", ".omo"}
        )

    def test_overlapping_roots_share_watches_and_release_them(self):
        self.mk("src", "lib")
        self.w.add_root(self.root)
        self.w.add_root(os.path.join(self.root, "src"))
        self.w.add_root(self.root)  # second tab on the same worktree
        self.assertEqual(len(self.fake.by_path), 3)

        self.w.remove_root(os.path.join(self.root, "src"))
        self.assertEqual(self.fake.removed, [])  # still covered by root
        self.w.remove_root(self.root)
        self.assertEqual(self.fake.removed, [])  # one reference left
        self.w.remove_root(self.root)
        self.assertEqual(self.fake.by_path, {})
        self.assertEqual(self.w.watch_count, 0)


class EventTests(WatcherTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.mk("src")
        self.w.add_root(self.root)

    def test_changes_are_reported_with_their_folder_then_cleared(self):
        self.w.handle_events([(self.wd("src"), IN_CLOSE_WRITE, 0, "app.py")])
        src = os.path.join(self.root, "src")
        self.assertEqual(set(self.w.drain(self.root)), {os.path.join(src, "app.py"), src})
        self.assertEqual(self.w.drain(self.root), [])

    def test_new_folder_is_watched_and_its_early_files_reported(self):
        self.mk("src", "new", "deep")
        Path(self.root, "src", "new", "a.ts").write_text("x")
        self.w.handle_events([(self.wd("src"), IN_CREATE | IN_ISDIR, 0, "new")])
        self.w.run_pending_work()
        self.assertIn("src/new/deep", self.watched())
        changed = self.w.drain(self.root)
        self.assertIn(os.path.join(self.root, "src", "new", "a.ts"), changed)

    def test_new_node_modules_shows_up_but_is_not_descended(self):
        self.mk("node_modules", "pkg")
        self.w.handle_events([(self.wd(), IN_CREATE | IN_ISDIR, 0, "node_modules")])
        self.w.run_pending_work()
        self.assertNotIn("node_modules", self.watched())
        self.assertIn(os.path.join(self.root, "node_modules"), self.w.drain(self.root))

    def test_moved_folder_watches_follow_the_new_name(self):
        self.mk("src", "old", "inner")
        self.w.handle_events([(self.wd("src"), IN_CREATE | IN_ISDIR, 0, "old")])
        self.w.run_pending_work()
        os.rename(os.path.join(self.root, "src", "old"), os.path.join(self.root, "src", "renamed"))
        self.w.handle_events(
            [
                (self.wd("src"), IN_MOVED_FROM | IN_ISDIR, 7, "old"),
                (self.wd("src"), IN_MOVED_TO | IN_ISDIR, 7, "renamed"),
            ]
        )
        self.w.run_pending_work()
        self.assertNotIn("src/old", self.watched())
        self.assertIn("src/renamed/inner", self.watched())

    def test_deleted_folder_watch_is_forgotten(self):
        wd = self.wd("src")
        self.w.handle_events([(wd, IN_IGNORED, 0, "")])
        self.assertNotIn("src", self.watched())

    def test_event_storm_is_bounded(self):
        events = [
            (self.wd("src"), IN_CLOSE_WRITE, 0, f"f{i}.txt")
            for i in range(fswatch.MAX_PENDING_PER_ROOT * 3)
        ]
        self.w.handle_events(events)
        self.assertEqual(self.w.drain(self.root), [self.root])


class OverflowTests(WatcherTestCase):
    def test_overflow_storm_resyncs_once_per_interval_and_reports_the_root(self):
        self.mk("src")
        self.w.add_root(self.root)
        self.mk("src", "made_during_overflow")
        calls_before = self.fake.add_calls

        for _ in range(20):  # a storm: many overflows back to back
            self.w.handle_events([(-1, IN_Q_OVERFLOW, 0, "")])
            self.w.run_pending_work()

        walked = self.fake.add_calls - calls_before
        # One resync, and it only asks the kernel about the folder it lacked.
        self.assertEqual(walked, 1)
        self.assertIn("src/made_during_overflow", self.watched())
        self.assertEqual(self.w.drain(self.root), [self.root])
        # Watches were not torn down and rebuilt.
        self.assertEqual(self.fake.removed, [])

        # After the interval, a later overflow resyncs again.
        for root in self.w._roots.values():
            root.last_resync -= fswatch.RESYNC_MIN_INTERVAL
        self.mk("src", "later")
        os.rmdir(os.path.join(self.root, "src", "made_during_overflow"))
        self.w.handle_events([(-1, IN_Q_OVERFLOW, 0, "")])
        self.w.run_pending_work()
        self.assertIn("src/later", self.watched())
        self.assertNotIn("src/made_during_overflow", self.watched())

    def test_parse_events_round_trip(self):
        import struct

        name = b"a.txt\0\0\0"
        buf = struct.pack("iIII", 3, IN_CLOSE_WRITE, 0, len(name)) + name
        buf += struct.pack("iIII", -1, IN_Q_OVERFLOW, 0, 0)
        self.assertEqual(
            fswatch.parse_events(buf),
            [(3, IN_CLOSE_WRITE, 0, "a.txt"), (-1, IN_Q_OVERFLOW, 0, "")],
        )


class SubscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_subscriber_merges_instead_of_dropping(self):
        from cptr.routers.events import _Subscriber

        sub = _Subscriber("/r")
        for i in range(50):
            sub.push([f"/r/{i}"])
        self.assertEqual(len(sub.take()), 50)
        sub.push([f"/r/{i}" for i in range(fswatch.MAX_PENDING_PER_ROOT + 5)])
        sub.push(["/r/more"])
        self.assertEqual(sub.take(), ["/r"])
        self.assertFalse(sub.event.is_set())

    async def test_one_watch_per_folder_and_disposed_with_last_subscriber(self):
        from cptr.routers import events

        closed = []
        started = []

        def fake_start(path):
            started.append(path)
            return events._WatchEntry(lambda: [], lambda: closed.append(path), set())

        with mock.patch.object(events, "_start_watch", fake_start):
            p, a = await events._subscribe_to_path("/tmp")
            _, b = await events._subscribe_to_path("/tmp/")
            self.assertEqual(len(started), 1)
            await events._unsubscribe_from_path(p, a)
            self.assertEqual(closed, [])
            await events._unsubscribe_from_path(p, b)
            self.assertEqual(closed, [p])
            self.assertNotIn(p, events._watch_registry)


if __name__ == "__main__":
    unittest.main()
