"""Coordination Phase 3 tests: validation, integration runs, PR publication safeguards.

Real git repositories and real test commands (a tiny Python check script).
GitHub is replaced by a recording stub of ``gh``; a local bare repository
stands in for ``origin`` so ``git push`` is real.

Run: uv run python -m unittest tests.test_coordination_phase3 -v
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path

from cptr.models import Config
from cptr.models.chats import Chat
from cptr.models.coordination import (
    CoordClaim,
    CoordConflict,
    CoordIntegration,
    CoordMessage,
    CoordTask,
)
from cptr.utils import gh
from cptr.utils.coordination import coordinator, integration, prs, registry, validation
from cptr.utils.identity import identity_for_user_id

from tests.test_coordination import USER, git
from tests.test_coordination_phase2 import Phase2TestCase

CHECK = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('shared', 'shared.py')\n"
    "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
    "extra = getattr(mod, 'EXTRA', 0)\n"
    "sys.exit(0 if mod.VALUE > 0 and extra >= 0 else 1)\n"
)
TEST_CMD = f"{shlex.quote(sys.executable)} check.py"


class Phase3TestCase(Phase2TestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / "check.py").write_text(CHECK)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "add check")

    async def ready_task(self, title: str, edits: dict[str, str], **kw) -> dict:
        task = await self.task(title, test_command=TEST_CMD, **kw)
        for path, content in edits.items():
            self.write(task, path, content)
        await registry.set_status(task["id"], "running")
        await registry.set_status(task["id"], "reviewing")
        result = await validation.validate_task(task["id"])
        self.assertEqual(result["status"], "passed", result.get("output_tail"))
        return (await CoordTask.get_by_id(task["id"])).to_dict()

    async def integrate(self, **kw) -> dict:
        run = await integration.start(user_id=USER, repo_key=self.repo_key, **kw)
        result = await integration.wait(run["id"])
        await coordinator.wait_idle()
        return result

    @property
    def repo_key(self) -> str:
        return str(Path(self.repo, ".git").resolve())


class ValidationTests(Phase3TestCase):
    async def test_isolated_validation_records_result_at_head(self):
        good = await self.ready_task("Good change", {"shared.py": "VALUE = 3\n"})
        isolated = good["validation"]["isolated"]
        self.assertEqual((isolated["status"], isolated["scope"]), ("passed", "full"))
        self.assertEqual(isolated["head"], git(good["worktree_path"], "rev-parse", "HEAD"))
        bad = await self.task("Bad change", test_command=TEST_CMD)
        self.write(bad, "shared.py", "VALUE = -1\n")
        result = await validation.validate_task(bad["id"])
        self.assertEqual((result["status"], result["exit_code"]), ("failed", 1))

    async def test_targeted_command_gets_changed_files(self):
        task = await self.task("Targeted", targeted_test_command="echo {files}")
        self.write(task, "a b.py", "x = 1\n")
        result = await validation.validate_task(task["id"])
        self.assertEqual(result["scope"], "targeted")
        self.assertIn("a b.py", result["output_tail"])

    async def test_no_tests_configured(self):
        task = await self.task("Untested")
        self.write(task, "x.txt", "x\n")
        self.assertEqual((await validation.validate_task(task["id"]))["status"], "no_tests")

    async def test_timeout_kills_command(self):
        task = await self.task(
            "Slow", test_command=f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(60)'"
        )
        self.write(task, "x.txt", "x\n")
        validation_timeout = validation.timeout_seconds

        async def short():
            return 1

        validation.timeout_seconds = short
        try:
            result = await validation.validate_task(task["id"])
        finally:
            validation.timeout_seconds = validation_timeout
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["status"], "failed")

    async def test_readiness_reasons(self):
        task = await self.ready_task("Ready one", {"shared.py": "VALUE = 4\n"})
        current = await CoordTask.get_by_id(task["id"])
        self.assertEqual(await validation.readiness(current, {task["id"]}), [])
        Path(task["worktree_path"], "wip.txt").write_text("dirty")
        reasons = await validation.readiness(current, {task["id"]})
        self.assertIn("worktree has uncommitted changes", reasons)
        git(task["worktree_path"], "add", "-A")
        git(task["worktree_path"], "commit", "-qm", "more")
        reasons = await validation.readiness(await CoordTask.get_by_id(task["id"]), {task["id"]})
        self.assertIn("validation is stale (branch changed since tests ran)", reasons)
        fresh = await self.task("Not reviewed")
        reasons = await validation.readiness(await CoordTask.get_by_id(fresh["id"]), set())
        self.assertIn("status is queued (needs reviewing)", reasons)
        self.assertIn("no commits beyond the base", reasons)

    async def test_isolated_pass_is_not_integration_ready(self):
        task = await self.ready_task("Only isolated", {"shared.py": "VALUE = 6\n"})
        self.assertEqual(task["status"], "reviewing")


class IntegrationTests(Phase3TestCase):
    async def test_dependency_order_traceability_and_no_base_changes(self):
        main_before = git(self.repo, "rev-parse", "main")
        a = await self.ready_task("Schema", {"schema.py": "SCHEMA = 1\n"})
        c = await self.ready_task("Readme", {"README.md": "docs\n"})
        b = await self.ready_task(
            "Uses schema", {"user.py": "from schema import SCHEMA\n"}, depends_on=[a["id"]]
        )
        heads = {t["id"]: git(t["worktree_path"], "rev-parse", "HEAD") for t in (a, b, c)}
        run = await self.integrate()
        self.assertEqual(run["status"], "passed", run["summary"])
        self.assertEqual(run["task_order"], [a["id"], c["id"], b["id"]])
        self.assertEqual(run["tests"]["exit_code"], 0)
        for t in (a, b, c):
            done = await CoordTask.get_by_id(t["id"])
            self.assertEqual(done.status, "integration_ready")
            self.assertEqual(done.validation["integration"]["run_id"], run["id"])
            self.assertEqual(git(t["worktree_path"], "rev-parse", "HEAD"), heads[t["id"]])
        self.assertEqual(git(self.repo, "rev-parse", "main"), main_before)  # never merged into main
        merges = git(
            run["worktree_path"], "log", "--merges", "--format=%s", f"{run['base_sha']}..HEAD"
        )
        self.assertEqual(len(merges.splitlines()), 3)
        self.assertIn(a["id"], merges)
        self.assertTrue(Path(run["worktree_path"]).is_dir())  # kept for inspection
        self.assertEqual(await CoordClaim.for_task(run["id"]), [])  # lock released

    async def test_unresolved_confirmed_conflict_is_held_back_before_integration(self):
        a = await self.ready_task("Raise", {"shared.py": "VALUE = 2\n"})
        b = await self.ready_task("Lower", {"shared.py": "VALUE = 9\n"})
        await coordinator.refresh(self.repo_key)
        run = await self.integrate()
        excluded = {s["task_id"]: s for s in run["steps"] if s["result"] == "excluded"}
        self.assertEqual(set(excluded), {a["id"], b["id"]})
        self.assertIn(
            "unresolved confirmed conflict (merge_conflict)", excluded[a["id"]]["reasons"]
        )
        self.assertEqual(run["status"], "failed")

    async def test_merge_conflict_during_integration_requests_correction(self):
        chat = await Chat.create(USER, "agent", meta={})
        a = await self.ready_task("Raise again", {"shared.py": "VALUE = 2\n"})
        b = await self.ready_task("Lower again", {"shared.py": "VALUE = 9\n"}, chat_id=chat.id)
        d = await self.ready_task("After lower", {"after.py": "X = 1\n"}, depends_on=[b["id"]])
        await coordinator.refresh(self.repo_key)
        for row in await CoordConflict.query(self.repo_key, statuses={"open"}):
            await CoordConflict.set_status(row.id, "acknowledged")  # user: try anyway
        run = await self.integrate()
        steps = {s["task_id"]: s for s in run["steps"]}
        self.assertEqual(steps[a["id"]]["result"], "merged")
        self.assertEqual(steps[b["id"]]["result"], "conflict")
        self.assertEqual(steps[b["id"]]["files"], ["shared.py"])
        self.assertEqual(steps[b["id"]]["with"], [a["id"]])
        self.assertEqual(steps[d["id"]]["result"], "skipped")
        blocked = await CoordTask.get_by_id(b["id"])
        self.assertEqual(
            (blocked.status, blocked.blockers[0]["kind"]), ("blocked", "needs_correction")
        )
        self.assertEqual((await CoordTask.get_by_id(a["id"])).status, "integration_ready")
        self.assertEqual((await CoordTask.get_by_id(d["id"])).status, "reviewing")
        notes = await CoordMessage.pending_for(b["id"])
        self.assertTrue(any("could not merge your branch" in m.body for m in notes))
        self.assertEqual(git(run["worktree_path"], "status", "--porcelain"), "")  # merge aborted
        # The correction-turn hook moves it back to running and the conflict survives scans.
        await coordinator.on_turn_start(chat.id, (await Chat.get_by_id(chat.id)).meta)
        self.assertEqual((await CoordTask.get_by_id(b["id"])).status, "running")
        await coordinator.refresh(self.repo_key)
        kinds = {r.kind for r in await CoordConflict.query(self.repo_key, statuses={"open"})}
        self.assertIn("integration_merge", kinds)

    async def test_combined_test_failure_is_attributed_to_first_breaking_merge(self):
        a = await self.ready_task("Fine first", {"one.py": "A = 1\n"})
        b = await self.ready_task("Breaks combined", {"extra.py": "x = 1\n"})
        c = await self.ready_task("Fine last", {"two.py": "B = 1\n"})
        # B passes alone, but adds a hook that only fails once A's file exists too.
        self.write(
            b, "shared.py", "import os\nVALUE = 1\nEXTRA = -1 if os.path.exists('one.py') else 0\n"
        )
        await validation.validate_task(b["id"])
        run = await self.integrate()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["tests"]["attributed_to"], b["id"])
        self.assertEqual((await CoordTask.get_by_id(a["id"])).status, "integration_ready")
        culprit = await CoordTask.get_by_id(b["id"])
        self.assertEqual(culprit.blockers[0]["detail"], "integration tests failed")
        self.assertEqual((await CoordTask.get_by_id(c["id"])).status, "reviewing")
        note = next(s for s in run["steps"] if s.get("task_id") == c["id"])
        self.assertIn("not verified", note["note"])

    async def test_without_test_command_nothing_becomes_ready(self):
        task = await self.task("No tests here")
        self.write(task, "x.txt", "x\n")
        await registry.set_status(task["id"], "running")
        await registry.set_status(task["id"], "reviewing")
        await validation.validate_task(task["id"])
        run = await self.integrate()
        self.assertIn("not validated", " ".join(run["steps"][0]["reasons"]))
        await Config.upsert({"coordination.allow_untested": True})
        run = await self.integrate()
        self.assertEqual(run["status"], "passed")
        self.assertIn("allowed", run["summary"])
        self.assertEqual((await CoordTask.get_by_id(task["id"])).status, "integration_ready")

    async def test_only_one_integration_at_a_time(self):
        await self.ready_task("Solo", {"solo.py": "S = 1\n"})
        first, second = await asyncio.gather(
            integration.start(user_id=USER, repo_key=self.repo_key),
            integration.start(user_id=USER, repo_key=self.repo_key),
            return_exceptions=True,
        )
        errors = [r for r in (first, second) if isinstance(r, Exception)]
        runs = [r for r in (first, second) if isinstance(r, dict)]
        self.assertEqual(len(errors), 1)
        self.assertIn("Another integration is running", str(errors[0]))
        await integration.wait(runs[0]["id"])

    async def test_interrupted_run_is_reconciled(self):
        run = await CoordIntegration.create(user_id=USER, repo_key=self.repo_key, status="running")
        await CoordClaim.claim(self.repo_key, run.id, "integration", "integration:active")
        self.assertEqual(await integration.reconcile(), 1)
        self.assertEqual((await CoordIntegration.get_by_id(run.id)).status, "interrupted")
        self.assertEqual(await CoordClaim.for_task(run.id), [])

    def test_topo_order(self):
        order = integration.topo_order(
            ["c", "b", "a"], [("b", "a"), ("c", "b"), ("c", "zz")], {"a": 3, "b": 2, "c": 1}
        )
        self.assertEqual(order, ["a", "b", "c"])


class PrTests(Phase3TestCase):
    def setUp(self) -> None:
        super().setUp()
        remote = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", str(remote))
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-q", "origin", "main")
        self.gh_calls: list[list[str]] = []
        self.prs: dict[str, dict] = {}
        self._real = gh.run_gh

        async def fake_gh(args, *, identity, cwd=None, check=True, timeout=20):
            self.gh_calls.append(list(args))
            if args[:2] == ["pr", "list"]:
                head = args[args.index("--head") + 1]
                return 0, json.dumps([p for p in self.prs.values() if p["headRefName"] == head]), ""
            if args[:2] == ["pr", "create"]:
                head = args[args.index("--head") + 1]
                number = len(self.prs) + 1
                self.prs[head] = {
                    "number": number,
                    "url": f"https://example.test/pr/{number}",
                    "state": "OPEN",
                    "isDraft": False,
                    "headRefName": head,
                    "baseRefName": args[args.index("--base") + 1],
                }
                self.last_body = args[args.index("--body") + 1]
                return 0, self.prs[head]["url"] + "\n", ""
            if args[:2] == ["pr", "view"]:
                ref = args[2]
                for p in self.prs.values():
                    if ref in {p["url"], str(p["number"]), p["headRefName"]}:
                        return 0, json.dumps(p), ""
                return 1, "", "not found"
            raise AssertionError(f"unexpected gh call {args}")

        gh.run_gh = fake_gh

    async def asyncSetUp(self) -> None:
        self.identity = await identity_for_user_id(USER)

    def tearDown(self) -> None:
        gh.run_gh = self._real
        super().tearDown()

    async def integrated(self, title="Publish me") -> dict:
        task = await self.ready_task(title, {"pub.py": "P = 1\n"})
        run = await self.integrate()
        self.assertEqual(run["status"], "passed", run["summary"])
        return (await CoordTask.get_by_id(task["id"])).to_dict()

    async def test_requires_user_confirmation_and_integration(self):
        task = await self.ready_task("Too early", {"e.py": "E = 1\n"})
        with self.assertRaises(prs.PrError):
            await prs.ensure_pr(task["id"], identity=self.identity, user_confirmed=False)
        with self.assertRaises(prs.PrError) as ctx:
            await prs.ensure_pr(task["id"], identity=self.identity, user_confirmed=True)
        self.assertIn("integration-ready", str(ctx.exception))
        self.assertEqual(self.gh_calls, [])

    async def test_creates_once_then_reuses_even_after_restart(self):
        task = await self.integrated()
        first = await prs.ensure_pr(task["id"], identity=self.identity, user_confirmed=True)
        self.assertTrue(first["created"])
        self.assertEqual(first["pr"]["baseRefName"], "main")
        remote_head = git(self.tmp / "origin.git", "rev-parse", task["branch"])
        self.assertEqual(remote_head, git(task["worktree_path"], "rev-parse", "HEAD"))
        self.assertIn(task["id"], self.last_body)
        self.assertIn("Merging is left to a human", self.last_body)
        second = await prs.ensure_pr(task["id"], identity=self.identity, user_confirmed=True)
        self.assertFalse(second["created"])
        # Lose the recorded PR number (e.g. crash before saving): found by branch, not duplicated.
        await CoordTask.update_fields(task["id"], pr_number=None, pr_url=None)
        third = await prs.ensure_pr(task["id"], identity=self.identity, user_confirmed=True)
        self.assertFalse(third["created"])
        self.assertEqual(len(self.prs), 1)
        self.assertEqual(sum(c[:2] == ["pr", "create"] for c in self.gh_calls), 1)
        stored = await CoordTask.get_by_id(task["id"])
        self.assertEqual((stored.pr_number, stored.pr_state), (1, "OPEN"))

    async def test_coordinator_can_never_merge(self):
        for forbidden in (["pr", "merge", "1"], ["pr", "close", "1"]):
            with self.assertRaises(prs.PrError):
                await prs._gh(forbidden, identity=self.identity, cwd=str(self.repo))
        task = await self.integrated("Never merged")
        await prs.ensure_pr(task["id"], identity=self.identity, user_confirmed=True)
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls))
        self.assertNotEqual(
            git(self.tmp / "origin.git", "rev-parse", "main"),
            git(self.tmp / "origin.git", "rev-parse", task["branch"]),
        )

    def test_no_merge_code_path_in_coordination_package(self):
        package = Path(prs.__file__).parent
        for source in package.glob("*.py"):
            text = source.read_text()
            self.assertNotIn('"pr", "merge"', text.replace('("pr", "merge")', ""), source.name)
