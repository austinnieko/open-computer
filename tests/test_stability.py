"""Regression tests for the performance and stability audit.

Run: uv run python -m unittest tests.test_stability -v
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest import mock

from cptr.models.chats import Chat
from cptr.routers import events
from cptr.utils import git as git_utils

from tests.test_coordination import USER, CoordinationTestCase


class TimerQueryTests(CoordinationTestCase):
    async def test_due_timers_filtered_in_sql_still_match_python_rules(self):
        now = time.time_ns()
        due = await Chat.create(
            USER,
            "due",
            meta={"internal": True, "type": "timer", "status": "pending", "timer_at": now - 1},
        )
        await Chat.create(  # not due yet
            USER,
            "later",
            meta={"internal": True, "type": "timer", "status": "pending", "timer_at": now + 10**12},
        )
        await Chat.create(  # already fired
            USER,
            "fired",
            meta={"internal": True, "type": "timer", "status": "completed", "timer_at": now - 1},
        )
        await Chat.create(USER, "plain chat", meta={"workspace": "/x"})
        await Chat.create(USER, "subagent", meta={"internal": True, "type": "subagent"})

        self.assertEqual([c.id for c in await Chat.get_due_timers(now)], [due.id])

    async def test_pending_timers_for_parent(self):
        mine = await Chat.create(
            USER,
            "t",
            meta={"internal": True, "type": "timer", "status": "pending", "parent_chat_id": "p1"},
        )
        await Chat.create(
            USER,
            "t",
            meta={"internal": True, "type": "timer", "status": "pending", "parent_chat_id": "p2"},
        )
        self.assertEqual([c.id for c in await Chat.get_pending_timers("p1")], [mine.id])


class GitStatusTests(CoordinationTestCase):
    async def test_untracked_files_counted_off_loop_and_big_ones_skipped(self):
        repo = Path(self.repo)
        (repo / "small.txt").write_text("a\nb\nc\n")
        (repo / "blob.bin").write_bytes(b"\0\1\2")
        (repo / "huge.log").write_bytes(b"x\n" * (git_utils._COUNT_MAX_BYTES // 2 + 10))

        files = {f["path"]: f for f in (await git_utils.status(str(repo)))["files"]}
        self.assertEqual(files["small.txt"]["additions"], 3)
        self.assertTrue(files["blob.bin"].get("binary"))
        # Too big to count: listed, not mislabelled as binary, no made-up count.
        self.assertIn("huge.log", files)
        self.assertNotIn("additions", files["huge.log"])
        self.assertFalse(files["huge.log"].get("binary"))

    async def test_a_git_that_never_exits_is_killed(self):
        repo = str(self.repo)
        real_exec = asyncio.create_subprocess_exec

        async def hung_git(*_args, **kw):  # stands in for a git stuck on a lock or remote
            return await real_exec("sleep", "30", **kw)

        with mock.patch.object(git_utils, "_LOCAL_TIMEOUT", 0.5), mock.patch.object(
            git_utils.asyncio, "create_subprocess_exec", hung_git
        ):
            started = time.monotonic()
            with self.assertRaises(git_utils.GitError) as ctx:
                await git_utils._run("status", cwd=repo)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(ctx.exception.returncode, 124)

    def test_subcommand_skips_leading_options(self):
        self.assertEqual(git_utils._subcommand(("-c", "user.name=x", "merge", "b")), "merge")
        self.assertEqual(git_utils._subcommand(("push", "-u", "origin")), "push")
        self.assertEqual(git_utils._subcommand(("--version",)), "--version")


class PortScanTests(CoordinationTestCase):
    async def asyncSetUp(self) -> None:
        events._shared_scan = None

    async def test_no_terminal_means_no_scan(self):
        with mock.patch.object(events, "_scan_ports", mock.AsyncMock(return_value=[])) as scan:
            with mock.patch.dict(events.manager._sessions, {}, clear=True):
                self.assertEqual(await events._scan_ports_shared(), [])
        scan.assert_not_called()

    async def test_tabs_share_one_scan(self):
        result = [{"port": 3000, "pid": 1, "process": "node", "session_id": "s"}]
        with mock.patch.object(events, "_scan_ports", mock.AsyncMock(return_value=result)) as scan:
            with mock.patch.dict(events.manager._sessions, {"s": object()}, clear=True):
                got = await asyncio.gather(*[events._scan_ports_shared() for _ in range(5)])
        self.assertEqual(scan.await_count, 1)
        self.assertTrue(all(g == result for g in got))
