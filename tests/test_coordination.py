"""Coordination Phase 1 tests: tasks, claims, worktrees, session binding, recovery.

Uses real git repositories in temp dirs and a real SQLite database migrated
with Alembic. Agent sessions are simulated: nothing here proves concurrency of
real OpenCode agents.

Run: uv run python -m unittest tests.test_coordination -v
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

import cptr.utils.db as db
from cptr.models.chats import Chat
from cptr.models.coordination import CoordClaim, CoordTask
from cptr.utils.agents.opencode import _directory_header
from cptr.utils.chat_task import _session_matches_cwd
from cptr.utils.coordination import registry
from cptr.utils.coordination import worktrees as wt

USER = "user-1"


def git(cwd: str | Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(parent: Path, name: str = "proj") -> Path:
    repo = parent / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "Test")
    (repo / "shared.py").write_text("VALUE = 1\n")
    (repo / "README.md").write_text("hello\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


class CoordinationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(os.path.realpath(self._tmp.name))
        db_file = self.tmp / "app.db"
        db.DB_FILE = db_file
        db._engine = None
        db._async_session = None
        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(Path(db.__file__).parent.parent / "migrations"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
        command.upgrade(cfg, "head")
        registry._repo_locks.clear()
        self.repo = make_repo(self.tmp)

    async def asyncTearDown(self) -> None:
        if db._engine is not None:
            await db._engine.dispose()
        db._engine = None
        db._async_session = None

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def register(self, title: str, **kw):
        return await registry.register_task(
            user_id=USER, repo_path=str(self.repo), title=title, **kw
        )


class WorktreeTests(CoordinationTestCase):
    async def test_creates_isolated_branch_worktree_under_managed_root(self):
        result = await self.register("Add login form")
        task, worktree = result["task"], result["worktree"]
        self.assertFalse(result["reused"])
        self.assertTrue(worktree["created_branch"])
        path = Path(task["worktree_path"])
        self.assertTrue(path.is_dir())
        self.assertTrue(wt.is_within(str(path), wt.managed_root(str(self.repo))))
        self.assertEqual(git(path, "rev-parse", "--abbrev-ref", "HEAD"), task["branch"])
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(task["status"], "queued")
        self.assertIn("worktree_ready", task["meta"]["milestones"])

    async def test_reuses_existing_branch_without_recreating(self):
        git(self.repo, "branch", "feat/existing")
        result = await self.register("Existing work", branch="feat/existing")
        self.assertFalse(result["worktree"]["created_branch"])
        self.assertEqual(
            git(result["task"]["worktree_path"], "rev-parse", "--abbrev-ref", "HEAD"),
            "feat/existing",
        )

    async def test_reregistering_same_work_reuses_task_and_worktree(self):
        first = await self.register("Fix   the Parser!")
        second = await self.register("fix the parser")
        self.assertTrue(second["reused"])
        self.assertEqual(first["task"]["id"], second["task"]["id"])
        self.assertTrue(second["worktree"]["reused"])
        self.assertEqual(len(await CoordTask.query(user_id=USER)), 1)

    async def test_recreates_worktree_deleted_out_of_band_on_reuse(self):
        first = await self.register("Task A")
        path = first["task"]["worktree_path"]
        git(self.repo, "worktree", "remove", path)
        second = await self.register("Task A")
        self.assertTrue(second["reused"])
        self.assertTrue(Path(path).is_dir())
        self.assertFalse(second["worktree"]["created_branch"])

    async def test_branch_checked_out_in_main_checkout_is_not_reused(self):
        git(self.repo, "switch", "-q", "-c", "feat/here")
        result = await self.register("Here", branch="feat/here")
        self.assertIn("error", result["worktree"])
        self.assertEqual(result["task"]["status"], "blocked")
        self.assertEqual(result["task"]["blockers"][0]["kind"], "worktree")

    async def test_existing_non_worktree_directory_is_left_untouched(self):
        repo = wt.RepoIdentity(*(await self._ident()))
        target = Path(wt.default_worktree_path(str(self.repo), "occupied"))
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("precious")
        with self.assertRaises(wt.WorktreeError):
            await wt.ensure_worktree(repo, branch="task/occupied", path=str(target))
        self.assertEqual((target / "keep.txt").read_text(), "precious")

    async def _ident(self):
        r = await wt.repo_identity(str(self.repo))
        return r.key, r.main_root

    async def test_rejects_paths_outside_managed_root(self):
        repo = await wt.repo_identity(str(self.repo))
        for bad in [
            str(self.tmp / "elsewhere"),
            str(self.repo),
            wt.managed_root(str(self.repo)) + "/../../escape",
            "relative/path",
        ]:
            with self.subTest(path=bad), self.assertRaises(wt.WorktreeError):
                await wt.ensure_worktree(repo, branch="task/x", path=bad)

    async def test_rejects_symlink_escape(self):
        root = Path(wt.managed_root(str(self.repo)))
        root.mkdir(parents=True)
        outside = self.tmp / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside)
        with self.assertRaises(wt.WorktreeError):
            wt.validate_task_path(str(self.repo), str(root / "link" / "child"))

    async def test_rejects_unsafe_branch_names(self):
        for bad in ["-x", "main", "HEAD", "a..b", "bad name", ""]:
            with self.subTest(branch=bad), self.assertRaises(wt.WorktreeError):
                await wt.validate_branch_name(str(self.repo), bad)

    async def test_remove_requires_confirmation_and_refuses_dirty(self):
        result = await self.register("Removable")
        path = result["task"]["worktree_path"]
        repo = await wt.repo_identity(str(self.repo))
        with self.assertRaises(wt.WorktreeError):
            await wt.remove_worktree(repo, path, user_confirmed=False)
        Path(path, "wip.txt").write_text("uncommitted")
        with self.assertRaises(wt.WorktreeError):
            await wt.remove_worktree(repo, path, user_confirmed=True)
        self.assertTrue(Path(path, "wip.txt").exists())
        Path(path, "wip.txt").unlink()
        await wt.remove_worktree(repo, path, user_confirmed=True)
        self.assertFalse(Path(path).exists())
        # Branch (and its commits) survive removal.
        git(self.repo, "rev-parse", "--verify", result["task"]["branch"])


class ConcurrencyTests(CoordinationTestCase):
    async def test_concurrent_registration_of_same_task_yields_one_task(self):
        results = await asyncio.gather(*[self.register("Same task") for _ in range(8)])
        ids = {r["task"]["id"] for r in results}
        self.assertEqual(len(ids), 1)
        self.assertEqual(sum(not r["reused"] for r in results), 1)
        self.assertEqual(len(await CoordTask.query(user_id=USER)), 1)

    async def test_concurrent_distinct_tasks_get_distinct_worktrees(self):
        results = await asyncio.gather(*[self.register(f"Task {i}") for i in range(5)])
        paths = {r["task"]["worktree_path"] for r in results}
        branches = {r["task"]["branch"] for r in results}
        self.assertEqual(len(paths), 5)
        self.assertEqual(len(branches), 5)
        for path in paths:
            self.assertTrue(Path(path).is_dir())

    async def test_claims_are_exclusive_across_tasks_even_without_repo_lock(self):
        a = (await self.register("Owner A"))["task"]
        b = (await self.register("Owner B"))["task"]

        async def claim(task_id):
            try:
                await CoordClaim.claim(a["repo_key"], task_id, "file", "file:shared.py")
                return task_id
            except Exception as exc:  # noqa: BLE001
                return exc

        outcomes = await asyncio.gather(*[claim(t) for t in [a["id"], b["id"]] * 4])
        winners = {o for o in outcomes if isinstance(o, str)}
        self.assertEqual(len(winners), 1)
        # Re-claiming by the owner is idempotent.
        await CoordClaim.claim(a["repo_key"], winners.pop(), "file", "file:shared.py")

    async def test_explicit_branch_cannot_be_claimed_twice(self):
        await self.register("First", branch="feat/one")
        # A different title on the same branch is the same work: reuse, not a 2nd owner.
        again = await self.register("Different title", branch="feat/one")
        self.assertTrue(again["reused"])

    async def test_status_compare_and_set_rejects_stale_writer(self):
        task = (await self.register("CAS"))["task"]
        await registry.set_status(task["id"], "running", expect="queued")
        with self.assertRaises(registry.CoordinationError):
            await registry.set_status(task["id"], "paused", expect="queued")
        with self.assertRaises(registry.CoordinationError):
            await registry.set_status(task["id"], "queued")  # not an allowed transition
        done = await registry.set_status(task["id"], "completed")
        self.assertIn("completed", done["meta"]["milestones"])


class SessionBindingTests(CoordinationTestCase):
    async def test_bound_chat_resolves_to_its_worktree(self):
        chat = await Chat.create(USER, "agent", meta={"workspace": str(self.repo)})
        task = (await self.register("Bound task"))["task"]
        await registry.bind_chat(task["id"], chat.id)
        chat = await Chat.get_by_id(chat.id)
        self.assertEqual(chat.meta["workspace"], str(self.repo))  # still listed under project
        cwd = await registry.resolve_chat_cwd(chat.id, chat.meta)
        self.assertEqual(cwd, os.path.realpath(task["worktree_path"]))

    async def test_untrusted_cwd_in_chat_meta_is_ignored(self):
        chat = await Chat.create(USER, "agent", meta={"workspace": str(self.repo)})
        task = (await self.register("Victim"))["task"]
        await registry.bind_chat(task["id"], chat.id)
        forged = dict((await Chat.get_by_id(chat.id)).meta)
        forged["cwd"] = str(self.tmp)  # not the task's worktree
        self.assertIsNone(await registry.resolve_chat_cwd(chat.id, forged))
        other = await Chat.create(USER, "other", meta={})
        stolen = {"cwd": task["worktree_path"], "coord_task_id": task["id"]}
        self.assertIsNone(await registry.resolve_chat_cwd(other.id, stolen))

    async def test_one_chat_per_task(self):
        c1 = await Chat.create(USER, "a", meta={})
        c2 = await Chat.create(USER, "b", meta={})
        task = (await self.register("Single owner"))["task"]
        await registry.bind_chat(task["id"], c1.id)
        with self.assertRaises(registry.CoordinationError):
            await registry.bind_chat(task["id"], c2.id)

    async def test_one_task_per_chat(self):
        chat = await Chat.create(USER, "a", meta={})
        t1 = (await self.register("First job"))["task"]
        t2 = (await self.register("Second job"))["task"]
        await registry.bind_chat(t1["id"], chat.id)
        with self.assertRaises(registry.CoordinationError):
            await registry.bind_chat(t2["id"], chat.id)

    async def test_record_session_persists_agent_session(self):
        task = (await self.register("Sess"))["task"]
        await registry.record_session(task["id"], agent="opencode", session_id="ses_1")
        reloaded = await CoordTask.get_by_id(task["id"])
        self.assertEqual((reloaded.agent, reloaded.agent_session_id), ("opencode", "ses_1"))

    def test_resume_only_in_same_directory(self):
        self.assertTrue(_session_matches_cwd({"workspace": "/a/b"}, "/a/b/"))
        self.assertFalse(_session_matches_cwd({"workspace": "/a/b"}, "/a/c"))
        self.assertTrue(_session_matches_cwd({"session_id": "x"}, "/a/c"))  # legacy record

    def test_opencode_v1_directory_header(self):
        self.assertEqual(_directory_header("/w/t"), {"x-opencode-directory": "/w/t"})
        self.assertEqual(_directory_header(""), {})
        self.assertEqual(_directory_header("/w/tâche"), {})


class RecoveryTests(CoordinationTestCase):
    async def _reopen_db(self):
        """Simulate a process restart: drop engine and in-memory locks."""
        await db._engine.dispose()
        db._engine = None
        db._async_session = None
        registry._repo_locks.clear()

    async def test_running_task_without_live_chat_is_paused_after_restart(self):
        chat = await Chat.create(USER, "agent", meta={})
        task = (await self.register("Long job", chat_id=chat.id))["task"]
        await registry.set_status(task["id"], "running")
        await self._reopen_db()
        counts = await registry.reconcile(active_chat_ids=set())
        self.assertEqual(counts["paused"], 1)
        reloaded = await CoordTask.get_by_id(task["id"])
        self.assertEqual(reloaded.status, "paused")
        self.assertEqual(reloaded.meta["status_reason"], "reconciled after restart")

    async def test_running_task_with_live_chat_is_untouched(self):
        chat = await Chat.create(USER, "agent", meta={})
        task = (await self.register("Live job", chat_id=chat.id))["task"]
        await registry.set_status(task["id"], "running")
        await registry.reconcile(active_chat_ids={chat.id})
        self.assertEqual((await CoordTask.get_by_id(task["id"])).status, "running")

    async def test_missing_worktree_blocks_then_unblocks_without_losing_claims(self):
        task = (await self.register("Fragile"))["task"]
        await registry.set_status(task["id"], "running")
        git(self.repo, "worktree", "remove", task["worktree_path"])
        await registry.reconcile(active_chat_ids=set())
        blocked = await CoordTask.get_by_id(task["id"])
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(blocked.blockers[0]["kind"], "worktree_missing")
        self.assertTrue(await CoordClaim.for_task(task["id"]))  # never auto-released
        # Re-registering restores the worktree; the next reconcile clears the blocker.
        await self.register("Fragile")
        await registry.reconcile(active_chat_ids=set())
        recovered = await CoordTask.get_by_id(task["id"])
        self.assertEqual(recovered.blockers, [])
        self.assertEqual(recovered.status, "paused")

    async def test_dirty_worktree_survives_reconcile_and_reregistration(self):
        task = (await self.register("Dirty"))["task"]
        Path(task["worktree_path"], "wip.py").write_text("x = 1\n")
        await self._reopen_db()
        await registry.reconcile(active_chat_ids=set())
        again = await self.register("Dirty")
        self.assertTrue(again["reused"])
        self.assertEqual(Path(task["worktree_path"], "wip.py").read_text(), "x = 1\n")

    async def test_no_duplicate_tasks_or_branches_after_restart(self):
        first = await self.register("Idempotent")
        await self._reopen_db()
        second = await self.register("Idempotent")
        self.assertEqual(first["task"]["id"], second["task"]["id"])
        branches = git(self.repo, "branch", "--list", "task/*").splitlines()
        self.assertEqual(len(branches), 1)

    async def test_failed_task_can_be_retried_and_completed_is_never_reused(self):
        task = (await self.register("Flaky"))["task"]
        await registry.set_status(task["id"], "failed", reason="tests failed")
        retry = await registry.set_status(task["id"], "queued")
        self.assertEqual(retry["status"], "queued")
        await registry.set_status(task["id"], "running")
        await registry.set_status(task["id"], "completed")
        fresh = await self.register("Flaky")
        self.assertFalse(fresh["reused"])
        self.assertNotEqual(fresh["task"]["id"], task["id"])


class MultiAgentSimulationTests(CoordinationTestCase):
    """Three simulated agents edit concurrently in their own worktrees.

    One pair deliberately edits the same line of ``shared.py``. Phase 1 must
    keep every agent's work isolated (no overwrite in any checkout); detecting
    the conflict before integration is Phase 2, but we confirm Git can see it.
    """

    async def test_three_agents_isolated_with_deliberate_conflict(self):
        chats = [await Chat.create(USER, f"agent {i}", meta={}) for i in range(3)]
        specs = [
            ("Raise shared value", "shared.py", "VALUE = 2\n"),
            ("Lower shared value", "shared.py", "VALUE = 0\n"),  # conflicts with #0
            ("Update readme", "README.md", "hello world\n"),
        ]
        registered = await asyncio.gather(
            *[
                self.register(title, chat_id=chat.id, agent="simulated")
                for (title, _, _), chat in zip(specs, chats)
            ]
        )
        tasks = [r["task"] for r in registered]

        async def agent(task, chat, file, content):
            chat = await Chat.get_by_id(chat.id)
            cwd = await registry.resolve_chat_cwd(chat.id, chat.meta)
            self.assertEqual(cwd, os.path.realpath(task["worktree_path"]))
            await registry.set_status(task["id"], "running")
            await asyncio.sleep(0)  # interleave
            Path(cwd, file).write_text(content)
            await asyncio.to_thread(git, cwd, "commit", "-qam", task["title"])
            await registry.set_status(task["id"], "reviewing")

        await asyncio.gather(
            *[agent(t, c, f, body) for t, c, (_, f, body) in zip(tasks, chats, specs)]
        )

        # Main checkout untouched; each worktree holds only its own change.
        self.assertEqual((self.repo / "shared.py").read_text(), "VALUE = 1\n")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        for task, (_, file, body) in zip(tasks, specs):
            self.assertEqual(Path(task["worktree_path"], file).read_text(), body)
            self.assertEqual(git(task["worktree_path"], "status", "--porcelain"), "")
        self.assertEqual(Path(tasks[2]["worktree_path"], "shared.py").read_text(), "VALUE = 1\n")

        def merges_cleanly(a, b) -> bool:
            proc = subprocess.run(
                ["git", "merge-tree", "--write-tree", a, b],
                cwd=self.repo,
                capture_output=True,
                text=True,
            )
            return proc.returncode == 0

        self.assertFalse(merges_cleanly(tasks[0]["branch"], tasks[1]["branch"]))
        self.assertTrue(merges_cleanly(tasks[0]["branch"], tasks[2]["branch"]))


if __name__ == "__main__":
    unittest.main()
