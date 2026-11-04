"""Coordination Phase 2 tests: conflict detection, effects, messages, scheduling.

Real git worktrees and a migrated SQLite DB; agent turns are simulated by
writing and committing files in each task's worktree. The scheduler's
"start a model turn" call is replaced by a recorder, so no model runs.

Run: uv run python -m unittest tests.test_coordination_phase2 -v
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from cptr.events import build_event
from cptr.models.chats import Chat
from cptr.models.coordination import CoordConflict, CoordMessage, CoordTask
from cptr.utils.coordination import changes as ch
from cptr.utils.coordination import conflicts, coordinator, messages, registry, scheduler
from cptr.utils.tools import coordinate, get_tool_list

from tests.test_coordination import USER, CoordinationTestCase, git


class Phase2TestCase(CoordinationTestCase):
    def setUp(self) -> None:
        super().setUp()
        coordinator._reset_for_tests()
        ch.clear_cache()
        conflicts._merge_cache.clear()
        self.launched: list[tuple[str, str]] = []

        async def launcher(task, start):
            self.launched.append((task.id, start["prompt"]))

        scheduler._set_launcher_for_tests(launcher)

    def tearDown(self) -> None:
        scheduler._set_launcher_for_tests(None)
        super().tearDown()

    async def task(self, title: str, **kw) -> dict:
        return (await self.register(title, **kw))["task"]

    def write(self, task: dict, path: str, content: str, *, commit: bool = True) -> None:
        target = Path(task["worktree_path"], path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        if commit:
            git(task["worktree_path"], "add", "-A")
            git(task["worktree_path"], "commit", "-qm", f"{task['title']}: {path}")

    async def conflicts_by_kind(self, repo_key: str) -> dict[str, CoordConflict]:
        rows = await CoordConflict.query(repo_key, statuses={"open", "acknowledged"})
        return {r.kind: r for r in rows}


class DetectionTests(Phase2TestCase):
    async def test_uncommitted_overlap_is_potential_and_disjoint_is_clean(self):
        a, b, c = await asyncio.gather(*(self.task(t) for t in ["Alpha", "Bravo", "Charlie"]))
        self.write(a, "shared.py", "VALUE = 2\n", commit=False)
        self.write(b, "shared.py", "VALUE = 3\n", commit=False)
        self.write(c, "docs/new.md", "doc\n", commit=False)
        await coordinator.refresh(a["repo_key"])
        found = await self.conflicts_by_kind(a["repo_key"])
        self.assertEqual(set(found), {"file_overlap"})
        row = found["file_overlap"]
        self.assertEqual(row.level, "potential")
        self.assertEqual({row.task_a, row.task_b}, {a["id"], b["id"]})
        self.assertEqual(row.evidence["files"], ["shared.py"])
        self.assertEqual(row.evidence["uncommitted"], ["shared.py"])

    async def test_committed_incompatible_edits_are_confirmed_by_merge_tree(self):
        a, b = await self.task("Raise value"), await self.task("Lower value")
        self.write(a, "shared.py", "VALUE = 2\n")
        self.write(b, "shared.py", "VALUE = 0\n")
        await coordinator.refresh(a["repo_key"])
        found = await self.conflicts_by_kind(a["repo_key"])
        self.assertEqual(found["merge_conflict"].level, "confirmed")
        self.assertEqual(found["merge_conflict"].evidence["files"], ["shared.py"])
        self.assertEqual(found["file_overlap"].level, "potential")

    async def test_same_file_compatible_edits_stay_potential(self):
        a, b = await self.task("Top"), await self.task("Bottom")
        base = "".join(f"line{i}\n" for i in range(20))
        git(self.repo, "switch", "-q", "main")
        (self.repo / "long.txt").write_text(base)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "long")
        # Re-base both task branches on the new main commit.
        for t in (a, b):
            git(t["worktree_path"], "merge", "-q", "--ff-only", "main")
        self.write(a, "long.txt", base.replace("line1\n", "LINE1\n"))
        self.write(b, "long.txt", base.replace("line18\n", "LINE18\n"))
        await coordinator.refresh(a["repo_key"])
        found = await self.conflicts_by_kind(a["repo_key"])
        self.assertIn("file_overlap", found)
        self.assertNotIn("merge_conflict", found)

    async def test_lockfile_config_and_migration_classification(self):
        a, b = await self.task("Dep one"), await self.task("Dep two")
        for t, n in ((a, "1"), (b, "2")):
            self.write(t, "uv.lock", f"lock {n}\n", commit=False)
            self.write(t, ".github/workflows/ci.yml", f"ci {n}\n", commit=False)
            self.write(t, f"app/migrations/versions/000{n}_x.py", "rev\n", commit=False)
        await coordinator.refresh(a["repo_key"])
        found = await self.conflicts_by_kind(a["repo_key"])
        self.assertEqual(found["lockfile"].evidence["files"], ["uv.lock"])
        self.assertEqual(found["shared_config"].evidence["files"], [".github/workflows/ci.yml"])
        self.assertEqual(found["migration_heads"].level, "potential")
        self.assertNotIn(
            "file_overlap", found, found.get("file_overlap") and found["file_overlap"].evidence
        )

    async def test_public_interface_change_used_by_other_task(self):
        git(self.repo, "switch", "-q", "main")
        (self.repo / "api.py").write_text("def detect(frame):\n    return frame\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "api")
        a, b = await self.task("Change detection API"), await self.task("Use detection")
        self.write(a, "api.py", "def detect(frame, threshold):\n    return frame\n")
        self.write(b, "consumer.py", "from api import detect\nprint(detect(1))\n")
        await coordinator.refresh(a["repo_key"])
        row = (await self.conflicts_by_kind(a["repo_key"]))["interface"]
        self.assertEqual(row.evidence["changed_by"], a["id"])
        self.assertEqual(row.evidence["used_by"], b["id"])
        self.assertEqual(row.evidence["symbols"], {"detect": ["api.py"]})

    async def test_dependency_version_mismatch(self):
        git(self.repo, "switch", "-q", "main")
        (self.repo / "package.json").write_text('{\n  "dependencies": {\n  }\n}\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "pkg")
        a, b = await self.task("Add lib v1"), await self.task("Add lib v2")
        self.write(a, "package.json", '{\n  "dependencies": {\n    "left-pad": "^1.0.0"\n  }\n}\n')
        self.write(b, "package.json", '{\n  "dependencies": {\n    "left-pad": "^2.0.0"\n  }\n}\n')
        await coordinator.refresh(a["repo_key"])
        row = (await self.conflicts_by_kind(a["repo_key"]))["dependency_version"]
        pkg = row.evidence["packages"][0]
        self.assertEqual(
            (pkg["package"], pkg[a["id"]], pkg[b["id"]]), ("left-pad", "^1.0.0", "^2.0.0")
        )

    async def test_base_branch_divergence_is_confirmed(self):
        a = await self.task("Feature on shared")
        self.write(a, "shared.py", "VALUE = 5\n")
        (self.repo / "shared.py").write_text("VALUE = 9\n")  # main moves incompatibly
        git(self.repo, "commit", "-qam", "main moved")
        await coordinator.refresh(a["repo_key"])
        row = (await self.conflicts_by_kind(a["repo_key"]))["base_divergence"]
        self.assertEqual(row.level, "confirmed")
        self.assertIsNone(row.task_b)
        self.assertEqual(row.evidence["base_branch"], "main")

    async def test_duplicate_requests_and_declared_overlap_are_informational(self):
        a = await self.task("Add user login page", files=["app/login.py"])
        b = await self.task("User login page add form", files=["app/login.py"])
        await coordinator.refresh(a["repo_key"])
        found = await self.conflicts_by_kind(a["repo_key"])
        self.assertEqual(found["duplicate_request"].level, "info")
        self.assertEqual(found["declared_overlap"].evidence["files"], ["app/login.py"])
        self.assertNotEqual(a["id"], b["id"])

    async def test_resolution_and_reopen(self):
        a, b = await self.task("One"), await self.task("Two")
        self.write(a, "shared.py", "VALUE = 2\n", commit=False)
        self.write(b, "shared.py", "VALUE = 3\n", commit=False)
        await coordinator.refresh(a["repo_key"])
        row = (await self.conflicts_by_kind(a["repo_key"]))["file_overlap"]
        git(b["worktree_path"], "checkout", "--", "shared.py")
        await coordinator.refresh(a["repo_key"])
        self.assertEqual((await CoordConflict.query_by_id(row.id))[0].status, "resolved")
        self.write(b, "shared.py", "VALUE = 4\n", commit=False)
        await coordinator.refresh(a["repo_key"])
        reopened = (await CoordConflict.query_by_id(row.id))[0]
        self.assertEqual(reopened.status, "open")  # same fingerprint, no duplicate row
        self.assertEqual(len(await CoordConflict.query(a["repo_key"])), 1)

    async def test_change_sets_are_cached_until_worktree_changes(self):
        a = await self.task("Cached")
        task = await CoordTask.get_by_id(a["id"])
        first = await ch.compute(task)
        self.assertIs(first, await ch.compute(task))
        self.write(a, "new.txt", "x\n", commit=False)
        second = await ch.compute(task)
        self.assertIsNot(first, second)
        self.assertEqual(second.uncommitted, {"new.txt": "A"})


class EffectTests(Phase2TestCase):
    async def test_blocking_conflict_pauses_only_the_dependent_task(self):
        chats = [await Chat.create(USER, f"c{i}", meta={}) for i in range(3)]
        a = await self.task("Base change", chat_id=chats[0].id)
        b = await self.task("Builds on base", chat_id=chats[1].id, depends_on=[a["id"]])
        c = await self.task("Unrelated edit", chat_id=chats[2].id)
        for t in (a, b, c):
            await registry.set_status(t["id"], "running")
        self.write(a, "shared.py", "VALUE = 2\n")
        self.write(b, "shared.py", "VALUE = 0\n")
        self.write(c, "shared.py", "VALUE = 7\n")
        await coordinator.refresh(a["repo_key"])

        statuses = {t: (await CoordTask.get_by_id(t)).status for t in (a["id"], b["id"], c["id"])}
        self.assertEqual(statuses, {a["id"]: "running", b["id"]: "blocked", c["id"]: "running"})
        blocked = await CoordTask.get_by_id(b["id"])
        self.assertEqual(blocked.blockers[0]["kind"], "conflict")
        self.assertEqual(blocked.meta["status_before_block"], "running")
        levels = {
            frozenset({r.task_a, r.task_b}): r.level
            for r in await CoordConflict.query(a["repo_key"])
            if r.kind == "merge_conflict"
        }
        self.assertEqual(levels[frozenset({a["id"], b["id"]})], "blocking")
        self.assertEqual(levels[frozenset({a["id"], c["id"]})], "confirmed")

        # B adopts A's value: the conflict disappears and B resumes (paused: no live turn).
        self.write(b, "shared.py", "VALUE = 2\n")
        await coordinator.refresh(a["repo_key"])
        resumed = await CoordTask.get_by_id(b["id"])
        self.assertEqual(resumed.status, "paused")
        self.assertEqual(resumed.blockers, [])
        self.assertNotIn("status_before_block", resumed.meta)

    async def test_acknowledging_a_blocking_conflict_releases_the_task(self):
        a = await self.task("Prereq ack")
        b = await self.task("Needs prereq ack", depends_on=[a["id"]])
        await registry.set_status(a["id"], "failed")
        await coordinator.refresh(a["repo_key"])
        row = (await self.conflicts_by_kind(a["repo_key"]))["dependency_failed"]
        self.assertEqual((await CoordTask.get_by_id(b["id"])).status, "blocked")
        await CoordConflict.set_status(row.id, "acknowledged")
        await coordinator.refresh(a["repo_key"])
        self.assertEqual((await CoordTask.get_by_id(b["id"])).status, "queued")
        # Still acknowledged on rescan: it is not re-blocked.
        await coordinator.refresh(a["repo_key"])
        self.assertEqual((await CoordTask.get_by_id(b["id"])).status, "queued")

    async def test_failed_dependency_blocks_dependent(self):
        a = await self.task("Prereq")
        b = await self.task("Needs prereq", depends_on=[a["id"]])
        await registry.set_status(a["id"], "failed")
        await coordinator.refresh(a["repo_key"])
        row = (await self.conflicts_by_kind(a["repo_key"]))["dependency_failed"]
        self.assertEqual((row.task_a, row.level), (b["id"], "blocking"))
        self.assertEqual((await CoordTask.get_by_id(b["id"])).status, "blocked")
        await registry.set_status(a["id"], "queued")  # retry
        await coordinator.refresh(a["repo_key"])
        self.assertEqual((await CoordTask.get_by_id(b["id"])).status, "queued")

    async def test_conflict_notifies_both_agents_once(self):
        a, b = await self.task("Left"), await self.task("Right")
        self.write(a, "shared.py", "VALUE = 2\n")
        self.write(b, "shared.py", "VALUE = 0\n")
        await coordinator.refresh(a["repo_key"])
        await coordinator.refresh(a["repo_key"])  # unchanged evidence: no new messages
        for t, other in ((a, b), (b, a)):
            pending = await CoordMessage.pending_for(t["id"])
            kinds = sorted(m.kind for m in pending)
            self.assertEqual(kinds, ["file_overlap", "incompatibility"])
            self.assertTrue(all(other["title"] in m.body for m in pending))


class MessageTests(Phase2TestCase):
    async def test_relevant_recipients_only(self):
        a = await self.task("Detection API")
        b = await self.task("Uses API", files=["api.py"])
        c = await self.task("Docs only", files=["README.md"])
        d = await self.task("Depends on API work", depends_on=[a["id"]])
        rows = await messages.post(
            from_task=a["id"],
            kind="interface_change",
            body="I am changing the detection API.",
            files=["api.py"],
        )
        self.assertEqual(sorted(r["to_task"] for r in rows), sorted([b["id"], d["id"]]))
        self.assertNotIn(c["id"], [r["to_task"] for r in rows])

    async def test_delivery_is_once_deduplicated_and_capped(self):
        chat = await Chat.create(USER, "agent", meta={})
        a = await self.task("Receiver", chat_id=chat.id)
        b = await self.task("Sender")
        for _ in range(3):
            await messages.post(
                from_task=b["id"], to_task=a["id"], kind="ready", body="Schema ready."
            )
        for i in range(12):
            await messages.post(from_task=b["id"], to_task=a["id"], kind="note", body=f"n{i}")
        meta = (await Chat.get_by_id(chat.id)).meta
        first = await coordinator.on_turn_start(chat.id, meta)
        self.assertIn("[Coordinator]", first)
        self.assertEqual(first.count("Schema ready."), 1)
        self.assertEqual(first.count("[note]"), messages.MAX_PER_DELIVERY - 1)
        second = await coordinator.on_turn_start(chat.id, meta)
        self.assertEqual(second.count("[note]"), 12 - (messages.MAX_PER_DELIVERY - 1))
        third = await coordinator.on_turn_start(chat.id, meta)
        self.assertNotIn("[note]", third)  # nothing left to deliver
        self.assertIn("CHECKOUT: done", third)  # every turn asks for a check-out
        self.assertEqual((await CoordTask.get_by_id(a["id"])).status, "running")

    async def test_concurrent_deliveries_never_duplicate(self):
        chat = await Chat.create(USER, "agent", meta={})
        a = await self.task("Racer", chat_id=chat.id)
        await messages.post(
            from_task=None, repo_key=a["repo_key"], to_task=a["id"], kind="note", body="only once"
        )
        blocks = await asyncio.gather(*(messages.take_delivery(a["id"]) for _ in range(6)))
        self.assertEqual(sum("only once" in b for b in blocks), 1)

    async def test_blocked_task_is_told_on_next_turn(self):
        chat = await Chat.create(USER, "agent", meta={})
        a = await self.task("Prereq two")
        b = await self.task("Dependent two", chat_id=chat.id, depends_on=[a["id"]])
        await registry.set_status(a["id"], "failed")
        await coordinator.refresh(a["repo_key"])
        block = await coordinator.on_turn_start(chat.id, (await Chat.get_by_id(chat.id)).meta)
        self.assertIn("BLOCKED", block)
        self.assertEqual((await CoordTask.get_by_id(b["id"])).status, "blocked")

    async def test_messages_survive_restart_undelivered(self):
        a, b = await self.task("Persist A"), await self.task("Persist B")
        await messages.post(from_task=a["id"], to_task=b["id"], kind="note", body="hello")
        await db_restart()
        self.assertEqual(len(await CoordMessage.pending_for(b["id"])), 1)


async def db_restart():
    import cptr.utils.db as db

    await db._engine.dispose()
    db._engine = None
    db._async_session = None
    registry._repo_locks.clear()
    coordinator._reset_for_tests()
    ch.clear_cache()


class SchedulerTests(Phase2TestCase):
    async def _chat_task(self, title, **kw):
        chat = await Chat.create(USER, title, meta={"last_model": "m"})
        return await self.task(title, chat_id=chat.id, start_prompt=f"do {title}", **kw)

    async def test_dependencies_hold_then_release(self):
        a = await self._chat_task("Schema")
        b = await self._chat_task("Use schema", depends_on=[a["id"]])
        await coordinator.refresh(a["repo_key"])
        self.assertEqual([t for t, _ in self.launched], [a["id"]])
        waiting = await CoordTask.get_by_id(b["id"])
        self.assertEqual(waiting.status, "queued")
        self.assertEqual(waiting.blockers[0]["kind"], "waiting_on")
        await registry.set_status(a["id"], "reviewing")
        await coordinator.refresh(a["repo_key"])
        self.assertEqual(len(self.launched), 1)  # reviewing is not verified
        await registry.set_status(a["id"], "integration_ready")
        await coordinator.refresh(a["repo_key"])
        self.assertEqual([t for t, _ in self.launched], [a["id"], b["id"]])
        started = await CoordTask.get_by_id(b["id"])
        self.assertEqual((started.status, started.blockers), ("running", []))
        self.assertNotIn("start", started.meta)

    async def test_writer_limit_defaults_to_four(self):
        tasks = [await self._chat_task(f"Parallel {i}") for i in range(5)]
        await coordinator.refresh(tasks[0]["repo_key"])
        self.assertEqual(len(self.launched), 4)
        statuses = sorted([(await CoordTask.get_by_id(t["id"])).status for t in tasks])
        self.assertEqual(statuses, ["queued", "running", "running", "running", "running"])

    async def test_slot_frees_when_a_writer_stops_running(self):
        tasks = [await self._chat_task(f"Slot {i}") for i in range(5)]
        await coordinator.refresh(tasks[0]["repo_key"])
        self.assertEqual(len(self.launched), 4)
        await registry.set_status(self.launched[0][0], "paused")
        await coordinator.refresh(tasks[0]["repo_key"])
        self.assertEqual(len(self.launched), 5)

    async def test_idle_agent_waits_for_user_and_frees_its_slot(self):
        tasks = [await self._chat_task(f"Idle {i}") for i in range(5)]
        await coordinator.refresh(tasks[0]["repo_key"])
        self.assertEqual(len(self.launched), 4)
        first = await CoordTask.get_by_id(self.launched[0][0])
        # Its turn ended and nothing else is running in its chat.
        self.assertEqual(await coordinator.on_turn_end(first.chat_id, grace=0), first.id)
        waiting = await CoordTask.get_by_id(first.id)
        self.assertEqual(waiting.status, "queued")
        self.assertIn("waiting_for_user", waiting.meta)
        await coordinator.refresh(tasks[0]["repo_key"])
        self.assertEqual(len(self.launched), 5)  # the freed slot went to the 5th task
        # The user's next message continues it.
        chat = await Chat.get_by_id(first.chat_id)
        await coordinator.on_turn_start(first.chat_id, chat.meta)
        resumed = await CoordTask.get_by_id(first.id)
        self.assertEqual(resumed.status, "running")
        self.assertNotIn("waiting_for_user", resumed.meta)
        self.assertEqual(len(self.launched), 5)  # never relaunched by the scheduler

    async def test_agent_still_working_is_not_marked_waiting(self):
        from unittest import mock

        a = await self._chat_task("Busy")
        await coordinator.refresh(a["repo_key"])
        chat_id = (await CoordTask.get_by_id(a["id"])).chat_id
        with mock.patch("cptr.utils.chat_task.get_active_chat_ids", return_value={chat_id}):
            self.assertIsNone(await coordinator.on_turn_end(chat_id, grace=0))
        self.assertEqual((await CoordTask.get_by_id(a["id"])).status, "running")

    async def test_subagent_turn_end_resolves_to_parent_task(self):
        a = await self._chat_task("Parent")
        await coordinator.refresh(a["repo_key"])
        parent = (await CoordTask.get_by_id(a["id"])).chat_id
        child = await Chat.create(USER, "explorer", meta={"parent_chat_id": parent})
        self.assertEqual(await coordinator.on_turn_end(child.id, grace=0), a["id"])

    async def test_restart_does_not_relaunch(self):
        a = await self._chat_task("Once")
        await coordinator.refresh(a["repo_key"])
        await db_restart()
        await registry.reconcile(active_chat_ids=set())  # running -> paused
        await registry.set_status(a["id"], "queued")
        await coordinator.refresh(a["repo_key"])
        self.assertEqual(len(self.launched), 1)

    async def test_cycle_rejected_and_surfaced(self):
        a, b, c = await self.task("Cy A"), await self.task("Cy B"), await self.task("Cy C")
        await scheduler.add_dependency(b["id"], a["id"])
        await scheduler.add_dependency(c["id"], b["id"])
        with self.assertRaises(registry.CoordinationError) as ctx:
            await scheduler.add_dependency(a["id"], c["id"])
        self.assertIn(" -> ", str(ctx.exception))
        rows = await CoordConflict.query(a["repo_key"])
        self.assertEqual([r.kind for r in rows], ["dependency_cycle"])
        await coordinator.refresh(a["repo_key"])  # cycles are not auto-resolved
        self.assertEqual((await CoordConflict.query_by_id(rows[0].id))[0].status, "open")

    def test_find_cycle(self):
        self.assertIsNone(scheduler.find_cycle([("b", "a")], ("c", "b")))
        self.assertEqual(
            scheduler.find_cycle([("b", "a"), ("c", "b")], ("a", "c")), ["a", "c", "b", "a"]
        )

    async def test_user_driven_tasks_are_not_autostarted(self):
        a = await self.task("Manual")
        await coordinator.refresh(a["repo_key"])
        self.assertEqual(self.launched, [])
        self.assertEqual((await CoordTask.get_by_id(a["id"])).status, "queued")


class TriggerAndToolTests(Phase2TestCase):
    async def test_turn_end_event_triggers_refresh(self):
        chat = await Chat.create(USER, "agent", meta={})
        a = await self.task("Evented", chat_id=chat.id)
        b = await self.task("Other")
        self.write(a, "shared.py", "VALUE = 2\n", commit=False)
        self.write(b, "shared.py", "VALUE = 3\n", commit=False)
        event = build_event("chat.finished", subject_id=chat.id, subject_type="chat")
        await coordinator.CoordinationEventSink().handle_event(event)
        await coordinator.wait_idle()
        self.assertIn("file_overlap", await self.conflicts_by_kind(a["repo_key"]))

    async def test_burst_of_events_is_coalesced(self):
        a = await self.task("Burst")
        calls = 0
        original = conflicts.scan

        async def counting(*args, **kw):
            nonlocal calls
            calls += 1
            return await original(*args, **kw)

        conflicts.scan = counting
        try:
            for _ in range(10):
                coordinator.request_refresh(a["repo_key"])
            await coordinator.wait_idle()
        finally:
            conflicts.scan = original
        self.assertLessEqual(calls, 2)

    async def test_coordinate_tool(self):
        chat = await Chat.create(USER, "agent", meta={})
        a = await self.task("Tool user", chat_id=chat.id)
        b = await self.task("Peer", files=["api.py"])
        ctx = {"chat_id": chat.id, "user_id": USER}
        status = json.loads(await coordinate("status", __context__=ctx))
        self.assertEqual(status["task"]["id"], a["id"])
        self.assertEqual([t["id"] for t in status["other_tasks"]], [b["id"]])
        sent = json.loads(
            await coordinate(
                "message",
                kind="interface_change",
                message="API v2",
                files=["api.py"],
                __context__=ctx,
            )
        )
        self.assertEqual(sent["delivered_to"], [b["id"]])
        await registry.set_status(a["id"], "running")
        ready = json.loads(await coordinate("ready", __context__=ctx))
        self.assertEqual(ready["status"], "reviewing")
        await coordinator.wait_idle()
        stranger = await Chat.create(USER, "plain", meta={})
        denied = json.loads(await coordinate("status", __context__={"chat_id": stranger.id}))
        self.assertIn("error", denied)

    async def test_coordinate_tool_only_offered_to_task_chats(self):
        plain = {t["name"] for t in await get_tool_list()}
        coordinated = {t["name"] for t in await get_tool_list(coordinated=True)}
        self.assertNotIn("coordinate", plain)
        self.assertIn("coordinate", coordinated)


if __name__ == "__main__":
    import unittest

    unittest.main()
