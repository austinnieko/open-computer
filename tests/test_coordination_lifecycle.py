"""Task lifecycle: agent check-out, Start, and handoff between dependent tasks.

Real git and a temp DB; agent turns are simulated (the scheduler's launcher is
replaced, and assistant replies are written straight to the chat).

Run: uv run python -m unittest tests.test_coordination_lifecycle -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from cptr.models import ChatMessage
from cptr.models.chats import Chat
from cptr.models.coordination import CoordTask
from cptr.utils.config import now_ms
from cptr.utils.coordination import coordinator, lifecycle

from tests.test_coordination import USER, git
from tests.test_coordination_api import ApiTestCase
from tests.test_coordination_phase2 import Phase2TestCase


class ParseCheckoutTests(Phase2TestCase):
    def test_forms(self):
        cases = {
            "All set.\nCHECKOUT: done - added the export button": (
                "done",
                "added the export button",
            ),
            "**CHECKOUT: needs-input** — which schema version?": (
                "needs-input",
                "which schema version?",
            ),
            "CHECKOUT: needs input: pick a name": ("needs-input", "pick a name"),
            "checkout: Blocked – waiting on the API key": ("blocked", "waiting on the API key"),
            "CHECKOUT: continuing": ("continuing", ""),
        }
        for text, (kind, note) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(lifecycle.parse_checkout(text), {"kind": kind, "note": note})

    def test_last_line_wins_and_missing_is_none(self):
        text = "CHECKOUT: continuing - exploring\n...\nCHECKOUT: done - finished"
        self.assertEqual(lifecycle.parse_checkout(text)["kind"], "done")
        self.assertIsNone(lifecycle.parse_checkout("No report here."))


class SettledTestCase(Phase2TestCase):
    async def asyncTearDown(self) -> None:
        # Let fire-and-forget refreshes (started by on_turn_end) finish first.
        pending = [t for t in coordinator._inflight.values() if not t.done()]
        await asyncio.gather(*pending, return_exceptions=True)
        await super().asyncTearDown()


class CheckoutTests(SettledTestCase):
    async def _running(self, title: str) -> CoordTask:
        chat = await Chat.create(USER, title, meta={"last_model": "m"})
        task = await self.task(title, chat_id=chat.id, start_prompt=f"do {title}")
        await coordinator.refresh(task["repo_key"])
        return await CoordTask.get_by_id(task["id"])

    async def _reply(self, chat_id: str, text: str) -> None:
        await ChatMessage.create(
            chat_id=chat_id, role="assistant", content=text, done=True, created_at=now_ms()
        )

    async def test_done_moves_to_review_with_handoff_note(self):
        task = await self._running("Export")
        await self._reply(
            task.chat_id, "Committed.\nCHECKOUT: done - export button added; docs left"
        )
        self.assertEqual(await coordinator.on_turn_end(task.chat_id, grace=0), task.id)
        task = await CoordTask.get_by_id(task.id)
        self.assertEqual(task.status, "reviewing")
        self.assertEqual(task.meta["checkout"]["kind"], "done")
        self.assertIn("docs left", task.meta["checkout"]["note"])
        self.assertNotIn("waiting_for_user", task.meta)

    async def test_needs_input_waits_for_user_with_the_question(self):
        task = await self._running("Schema")
        await self._reply(task.chat_id, "CHECKOUT: needs-input - v1 or v2?")
        await coordinator.on_turn_end(task.chat_id, grace=0)
        task = await CoordTask.get_by_id(task.id)
        self.assertEqual(task.status, "queued")
        self.assertIn("waiting_for_user", task.meta)
        self.assertEqual(
            task.meta["checkout"],
            {**task.meta["checkout"], "kind": "needs-input", "note": "v1 or v2?"},
        )

    async def test_no_checkout_is_reported_as_such(self):
        task = await self._running("Quiet")
        await self._reply(task.chat_id, "I looked around.")
        await coordinator.on_turn_end(task.chat_id, grace=0)
        task = await CoordTask.get_by_id(task.id)
        self.assertEqual(task.status, "queued")
        self.assertEqual(task.meta["checkout"]["kind"], "none")

    async def test_continuing_with_nothing_running_is_flagged_stalled(self):
        task = await self._running("Stall")
        await self._reply(task.chat_id, "CHECKOUT: continuing - waiting on explorers")
        await coordinator.on_turn_end(task.chat_id, grace=0)
        task = await CoordTask.get_by_id(task.id)
        self.assertEqual(task.status, "queued")
        self.assertTrue(task.meta["checkout"]["stalled"])

    async def test_every_turn_asks_for_a_checkout(self):
        task = await self._running("Ask")
        chat = await Chat.get_by_id(task.chat_id)
        block = await coordinator.on_turn_start(task.chat_id, chat.meta)
        self.assertIn("CHECKOUT: done", block)


class HandoffTests(SettledTestCase):
    async def test_dependent_starts_from_dependency_work_with_its_note(self):
        a_chat = await Chat.create(USER, "A", meta={"last_model": "m"})
        a = await self.task("Schema", chat_id=a_chat.id, start_prompt="do schema")
        self.write(a, "schema.sql", "create table t (id int);\n")
        b_chat = await Chat.create(USER, "B", meta={"last_model": "m"})
        b = await self.task(
            "Use schema", chat_id=b_chat.id, start_prompt="use it", depends_on=[a["id"]]
        )
        await coordinator.refresh(a["repo_key"])
        self.assertEqual([t for t, _ in self.launched], [a["id"]])  # B waits on A

        # A checks out done and passes integration.
        meta = lifecycle.checkout_meta(
            (await CoordTask.get_by_id(a["id"])).meta,
            {"kind": "done", "note": "table t exists; add indexes later"},
        )
        await CoordTask.update_fields(a["id"], status="integration_ready", meta=meta)
        await coordinator.refresh(a["repo_key"])
        self.assertIn(b["id"], [t for t, _ in self.launched])

        b_task = await CoordTask.get_by_id(b["id"])
        self.assertEqual(b_task.meta["dependency_merges"][0]["result"], "merged")
        self.assertTrue(Path(b["worktree_path"], "schema.sql").exists())
        self.assertIn("started_after", b_task.meta["milestones"])
        brief = await lifecycle.handoff_brief(b_task)
        self.assertIn("add indexes later", brief)
        self.assertIn("schema.sql", brief)
        self.assertIn("already in your folder", brief)

    async def test_conflicting_dependency_is_aborted_and_reported(self):
        a = await self.task("Left")
        b_chat = await Chat.create(USER, "B", meta={"last_model": "m"})
        b = await self.task("Right", chat_id=b_chat.id, start_prompt="go", depends_on=[a["id"]])
        self.write(a, "same.txt", "from A\n")
        self.write(b, "same.txt", "from B\n")
        await CoordTask.update_fields(a["id"], status="integration_ready")
        results = await lifecycle.prepare_from_dependencies(await CoordTask.get_by_id(b["id"]))
        self.assertEqual(results[0]["result"], "conflict")
        self.assertEqual(Path(b["worktree_path"], "same.txt").read_text(), "from B\n")
        self.assertEqual(git(b["worktree_path"], "status", "--porcelain"), "")


class StartRouteTests(ApiTestCase):
    async def test_draft_waits_for_start_then_gets_in_line(self):
        body = {
            "root": str(self.repo),
            "title": "Drafted",
            "create_chat": True,
            "model_id": "m",
            "start_prompt": "build it",
            "start_now": False,
        }
        task = (await self.post("/tasks", body)).json()["task"]
        self.assertNotIn("start", task["meta"])
        self.assertEqual(task["meta"]["draft_start"]["prompt"], "build it")

        started = (await self.post(f"/tasks/{task['id']}/start")).json()
        self.assertEqual(started["meta"]["start"]["prompt"], "build it")
        self.assertNotIn("draft_start", started["meta"])
        again = await self.post(f"/tasks/{task['id']}/start")
        self.assertEqual(again.status_code, 409)  # already in line

    async def test_start_without_instructions_uses_a_plain_prompt(self):
        body = {"root": str(self.repo), "title": "Plain", "create_chat": True, "model_id": "m"}
        task = (await self.post("/tasks", body)).json()["task"]
        started = (await self.post(f"/tasks/{task['id']}/start")).json()
        self.assertEqual(started["meta"]["start"]["prompt"], "Start working on this task.")


class RemoveDependencyTests(SettledTestCase):
    async def test_removing_a_dependency_lets_the_waiting_task_start(self):
        a = await self.task("First")
        b_chat = await Chat.create(USER, "B", meta={"last_model": "m"})
        b = await self.task("Second", chat_id=b_chat.id, start_prompt="go", depends_on=[a["id"]])
        await coordinator.refresh(a["repo_key"])
        self.assertNotIn(b["id"], [t for t, _ in self.launched])

        from cptr.utils.coordination import scheduler

        self.assertTrue(await scheduler.remove_dependency(b["id"], a["id"]))
        await coordinator.refresh(a["repo_key"])
        self.assertIn(b["id"], [t for t, _ in self.launched])
        self.assertEqual(await lifecycle.dependencies_of(await CoordTask.get_by_id(b["id"])), [])


class CheckinTests(SettledTestCase):
    _running = CheckoutTests._running
    _reply = CheckoutTests._reply

    async def _continuing(self, title: str) -> CoordTask:
        task = await self._running(title)
        await self._reply(task.chat_id, "CHECKOUT: continuing - reviewer sub-agent running")
        await coordinator.on_turn_end(task.chat_id, grace=0)
        return await CoordTask.get_by_id(task.id)

    async def test_continuing_sets_a_checkin_that_puts_the_task_back_in_line(self):
        task = await self._continuing("Review")
        due = task.meta["checkin_due"]
        self.assertIn(task.id, coordinator._checkins)
        self.assertTrue(await coordinator.run_checkin(task.id, due))
        await coordinator.wait_idle()
        task = await CoordTask.get_by_id(task.id)
        self.assertNotIn("waiting_for_user", task.meta)
        self.assertEqual(task.meta["checkins"], 1)  # survives the launch
        self.assertNotIn("checkin_due", task.meta)
        launched = [p for t, p in self.launched if t == task.id]
        self.assertTrue(launched and "Check-in" in launched[-1])

    async def test_a_reply_first_cancels_the_checkin(self):
        task = await self._continuing("Replied")
        due = task.meta["checkin_due"]
        chat = await Chat.get_by_id(task.chat_id)
        await coordinator.on_turn_start(task.chat_id, chat.meta)
        self.assertNotIn(task.id, coordinator._checkins)
        self.assertFalse(await coordinator.run_checkin(task.id, due))

    async def test_zero_turns_checkins_off(self):
        from cptr.models import Config

        await Config.upsert({"coordination.checkin_minutes": 0})
        task = await self._continuing("Off")
        self.assertNotIn("checkin_due", task.meta)
        self.assertNotIn(task.id, coordinator._checkins)

    async def test_checkins_stop_after_the_cap(self):
        task = await self._running("Capped")
        await CoordTask.update_fields(
            task.id, meta={**task.meta, "checkins": coordinator.MAX_CHECKINS}
        )
        await self._reply(task.chat_id, "CHECKOUT: continuing - still going")
        await coordinator.on_turn_end(task.chat_id, grace=0)
        self.assertNotIn("checkin_due", (await CoordTask.get_by_id(task.id)).meta)


class CheckinSettingTests(ApiTestCase):
    async def test_setting_round_trips_and_is_listed(self):
        url = "/api/coordination/limits"
        self.assertEqual((await self.client.get(url)).json()["checkin_minutes"], 10)
        r = await self.client.put(url, json={"checkin_minutes": 25})
        self.assertEqual(r.json()["checkin_minutes"], 25)
        self.assertEqual(r.json()["max_writers"], 4)  # untouched
        bad = await self.client.put(url, json={"checkin_minutes": 999})
        self.assertEqual(bad.status_code, 422)
        listed = (await self.client.get(f"/api/coordination/tasks?root={self.repo}")).json()
        self.assertEqual(listed["limits"]["checkin_minutes"], 25)


class MarkDoneTests(SettledTestCase):
    async def test_marking_an_ended_dependency_done_lets_the_dependent_start(self):
        from cptr.utils.coordination import registry

        a = await self.task("Schema")
        b_chat = await Chat.create(USER, "B", meta={"last_model": "m"})
        b = await self.task("Use it", chat_id=b_chat.id, start_prompt="go", depends_on=[a["id"]])
        await coordinator.refresh(a["repo_key"])  # B is in line, waiting on A
        self.assertIn("waiting_on", str((await CoordTask.get_by_id(b["id"])).blockers))
        # Ending the dependency (meant as "done") blocks the dependent.
        await registry.set_status(a["id"], "ended", reason="ended by user")
        await coordinator.refresh(a["repo_key"])
        blocked = await CoordTask.get_by_id(b["id"])
        self.assertNotIn(b["id"], [t for t, _ in self.launched])
        self.assertIn("dependency_failed", str(blocked.blockers))

        # As it happens in the app: the dependent is blocked and also waiting on A.
        self.assertIn("waiting_on", str(blocked.blockers))
        self.assertEqual(blocked.status, "blocked")

        done = await registry.mark_done(a["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["meta"]["milestones"]["completed"]["previous"], "ended")
        await coordinator.refresh(a["repo_key"])
        await coordinator.wait_idle()
        self.assertIn(b["id"], [t for t, _ in self.launched])
        b_task = await CoordTask.get_by_id(b["id"])
        self.assertEqual(b_task.status, "running")
        self.assertEqual(b_task.blockers, [])

    async def test_a_running_task_must_be_stopped_first(self):
        from cptr.utils.coordination import registry

        chat = await Chat.create(USER, "R", meta={"last_model": "m"})
        task = await self.task("Busy", chat_id=chat.id, start_prompt="go")
        await coordinator.refresh(task["repo_key"])
        self.assertEqual((await CoordTask.get_by_id(task["id"])).status, "running")
        with self.assertRaises(registry.CoordinationError):
            await registry.mark_done(task["id"])
