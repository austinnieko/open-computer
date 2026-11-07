"""Coordination REST API through the real app (auth middleware included).

Uses httpx's ASGI transport (no lifespan, no network). Real git, temp DB.

Run: uv run python -m unittest tests.test_coordination_api -v
"""

from __future__ import annotations

from pathlib import Path

import httpx

from cptr.models.chats import Chat
from cptr.models.coordination import CoordTask
from cptr.utils.config import create_token
from cptr.utils.coordination import coordinator

from tests.test_coordination import USER, git
from tests.test_coordination_phase2 import Phase2TestCase


class ApiTestCase(Phase2TestCase):
    async def asyncSetUp(self) -> None:
        from cptr.app import app

        self.app = app
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        self.client.cookies.set("cptr_session", create_token(USER, "tester", "admin"))

    async def asyncTearDown(self) -> None:
        await coordinator.wait_idle()
        await self.client.aclose()
        await super().asyncTearDown()

    async def post(self, path: str, body: dict | None = None, **kw):
        return await self.client.post(f"/api/coordination{path}", json=body or {}, **kw)


class ApiTests(ApiTestCase):
    async def test_requires_authentication(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        ) as anon:
            response = await anon.get(f"/api/coordination/tasks?root={self.repo}")
        self.assertEqual(response.status_code, 401)

    async def test_create_task_with_chat_then_list_and_diff(self):
        response = await self.post(
            "/tasks",
            {"root": str(self.repo), "title": "API task", "create_chat": True, "model_id": "m1"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["task"]
        chat = await Chat.get_by_id(task["chat_id"])
        self.assertEqual(chat.meta["workspace"], str(self.repo))
        self.assertEqual(chat.meta["cwd"], task["worktree_path"])
        self.assertEqual(chat.meta["last_model"], "m1")

        listed = (await self.client.get(f"/api/coordination/tasks?root={self.repo}")).json()
        self.assertEqual([t["id"] for t in listed["tasks"]], [task["id"]])
        self.assertEqual(listed["active_chats"], [])

        Path(task["worktree_path"], "shared.py").write_text("VALUE = 5\n")
        git(task["worktree_path"], "commit", "-qam", "edit")
        Path(task["worktree_path"], "new.txt").write_text("n\n")
        diff = (await self.client.get(f"/api/coordination/tasks/{task['id']}/diff")).json()
        self.assertEqual([f["path"] for f in diff["files"]], ["shared.py"])
        self.assertEqual(diff["untracked"], ["new.txt"])

    async def test_pause_and_resume_queue_a_continue_turn(self):
        task = (
            await self.post(
                "/tasks",
                {"root": str(self.repo), "title": "Pausable", "create_chat": True, "model_id": "m"},
            )
        ).json()["task"]
        await self.post(f"/tasks/{task['id']}/status", {"status": "running"})
        paused = (await self.post(f"/tasks/{task['id']}/pause")).json()
        self.assertEqual(paused["task"]["status"], "paused")
        resumed = await self.post(f"/tasks/{task['id']}/resume")
        self.assertEqual(resumed.status_code, 200, resumed.text)
        await coordinator.wait_idle()
        self.assertEqual([t for t, _ in self.launched], [task["id"]])
        self.assertIn("Continue this task", self.launched[0][1])
        self.assertEqual((await CoordTask.get_by_id(task["id"])).status, "running")

    async def test_other_users_tasks_are_invisible(self):
        task = (await self.post("/tasks", {"root": str(self.repo), "title": "Mine"})).json()["task"]
        self.client.cookies.set("cptr_session", create_token("someone-else", "other", "user"))
        self.assertEqual(
            (await self.client.get(f"/api/coordination/tasks/{task['id']}")).status_code, 404
        )
        self.assertEqual((await self.post(f"/tasks/{task['id']}/pause")).status_code, 404)
        listed = (await self.client.get(f"/api/coordination/tasks?root={self.repo}")).json()
        self.assertEqual(listed["tasks"], [])
        conflicts = (await self.client.get(f"/api/coordination/conflicts?root={self.repo}")).json()
        self.assertEqual(conflicts, {"conflicts": []})

    async def test_publish_and_remove_require_confirmation(self):
        task = (await self.post("/tasks", {"root": str(self.repo), "title": "Guarded"})).json()[
            "task"
        ]
        pr = await self.post(f"/tasks/{task['id']}/pr", {"confirm": False})
        self.assertEqual(pr.status_code, 409)
        self.assertIn("approval", pr.json()["detail"])
        await self.post(f"/tasks/{task['id']}/pause")
        removed = await self.post(f"/tasks/{task['id']}/worktree/remove", {"confirm": False})
        self.assertEqual(removed.status_code, 409)
        self.assertTrue(Path(task["worktree_path"]).is_dir())

    def test_no_merge_route_exists(self):
        from cptr.app import app

        paths = [
            getattr(r, "path", "")
            for r in app.routes
            if getattr(r, "path", "").startswith("/api/coordination")
        ]
        self.assertTrue(paths)
        self.assertFalse(any("merge" in p for p in paths), paths)


class EndDeleteTests(ApiTestCase):
    async def delete(self, task_id: str, body: dict):
        return await self.client.request("DELETE", f"/api/coordination/tasks/{task_id}", json=body)

    async def new_task(self, title: str, **kw) -> dict:
        body = {"root": str(self.repo), "title": title, "create_chat": True, "model_id": "m"}
        return (await self.post("/tasks", {**body, **kw})).json()["task"]

    async def test_end_then_delete_keeps_branch_and_detaches_chat(self):
        task = await self.new_task("Abandon me")
        ended = await self.post(f"/tasks/{task['id']}/end")
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual(ended.json()["task"]["status"], "ended")
        # Ended is final: it cannot be resumed or restarted.
        self.assertEqual((await self.post(f"/tasks/{task['id']}/resume")).status_code, 409)

        self.assertEqual((await self.delete(task["id"], {"confirm": False})).status_code, 409)
        gone = await self.delete(task["id"], {"confirm": True, "remove_worktree": True})
        self.assertEqual(gone.status_code, 200, gone.text)
        self.assertTrue(gone.json()["worktree_removed"])
        self.assertIsNone(await CoordTask.get_by_id(task["id"]))
        self.assertFalse(Path(task["worktree_path"]).exists())
        self.assertIn(task["branch"], git(self.repo, "branch", "--list", task["branch"]))
        chat = await Chat.get_by_id(task["chat_id"])
        self.assertNotIn("coord_task_id", chat.meta)
        self.assertNotIn("cwd", chat.meta)

    async def test_delete_refuses_unfinished_task_and_can_keep_folder(self):
        task = await self.new_task("Still going")
        refused = await self.delete(task["id"], {"confirm": True})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("end it first", refused.json()["detail"])
        await self.post(f"/tasks/{task['id']}/end")
        kept = await self.delete(task["id"], {"confirm": True, "remove_worktree": False})
        self.assertEqual(kept.status_code, 200, kept.text)
        self.assertTrue(Path(task["worktree_path"]).is_dir())

    async def test_delete_refuses_dirty_worktree_removal(self):
        task = await self.new_task("Dirty")
        Path(task["worktree_path"], "wip.txt").write_text("unsaved\n")
        await self.post(f"/tasks/{task['id']}/end")
        refused = await self.delete(task["id"], {"confirm": True, "remove_worktree": True})
        self.assertEqual(refused.status_code, 409)
        self.assertIsNotNone(await CoordTask.get_by_id(task["id"]))  # nothing deleted

    async def test_ending_a_dependency_blocks_dependents(self):
        prereq = await self.new_task("Prereq")
        dependent = await self.new_task("Dependent")
        await self.post(f"/tasks/{dependent['id']}/dependencies", {"depends_on": prereq["id"]})
        await self.post(f"/tasks/{prereq['id']}/end")
        await coordinator.wait_idle()
        await coordinator.refresh((await CoordTask.get_by_id(prereq["id"])).repo_key)
        self.assertEqual((await CoordTask.get_by_id(dependent["id"])).status, "blocked")

    async def test_first_turn_gets_task_brief_once(self):
        task = await self.new_task("Add export", description="Export results as CSV.")
        meta = (await Chat.get_by_id(task["chat_id"])).meta
        first = await coordinator.on_turn_start(task["chat_id"], meta)
        self.assertIn("Task: Add export", first)
        self.assertIn("Export results as CSV.", first)
        self.assertIn(task["worktree_path"], first)
        second = await coordinator.on_turn_start(task["chat_id"], meta)
        self.assertNotIn("Task: Add export", second)


class ChatFileTests(ApiTestCase):
    """Task chats get the sidebar chat file a normal new chat gets."""

    def _chat_file(self, chat_id: str) -> Path:
        return self.repo / ".cptr" / "chats" / f"{chat_id}.json"

    async def test_create_chat_writes_sidebar_file(self):
        body = {"root": str(self.repo), "title": "Listed", "create_chat": True, "model_id": "m"}
        task = (await self.post("/tasks", body)).json()["task"]
        self.assertTrue(self._chat_file(task["chat_id"]).exists())
        listed = (await self.client.get(f"/api/chats?workspace={self.repo}")).json()
        self.assertIn(task["chat_id"], [c["id"] for c in listed["chats"]])

    async def test_listing_backfills_missing_file_without_overwriting(self):
        from cptr.routers import coordination as coord_router

        body = {"root": str(self.repo), "create_chat": True, "model_id": "m"}
        old = (await self.post("/tasks", {**body, "title": "Old"})).json()["task"]
        kept = (await self.post("/tasks", {**body, "title": "Kept"})).json()["task"]
        self._chat_file(old["chat_id"]).unlink()  # as if created before the fix
        self._chat_file(kept["chat_id"]).write_text('{"id": "real"}')
        coord_router._chat_files_ok.clear()

        await self.client.get(f"/api/coordination/tasks?root={self.repo}")
        self.assertEqual(self._chat_file(old["chat_id"]).read_text(), "{}")
        self.assertEqual(self._chat_file(kept["chat_id"]).read_text(), '{"id": "real"}')


class DeleteTaskChatTests(ApiTestCase):
    """Deleting a task's agent chat removes the task; its branch and folder stay."""

    async def _task(self, title: str) -> dict:
        body = {"root": str(self.repo), "title": title, "create_chat": True, "model_id": "m"}
        return (await self.post("/tasks", body)).json()["task"]

    async def test_deleting_task_chat_removes_task_keeps_branch_and_folder(self):
        task = await self._task("Chat goes")
        response = await self.client.delete(f"/api/chats/{task['chat_id']}")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(await CoordTask.get_by_id(task["id"]))
        self.assertTrue(Path(task["worktree_path"]).is_dir())
        self.assertIn(task["branch"], git(self.repo, "branch", "--list", task["branch"]))

    async def test_task_with_missing_chat_is_flagged_and_deletable_unended(self):
        task = await self._task("Orphan")
        await Chat.delete(task["chat_id"])  # as before chat deletion removed tasks
        listed = (await self.client.get(f"/api/coordination/tasks?root={self.repo}")).json()
        row = next(t for t in listed["tasks"] if t["id"] == task["id"])
        self.assertTrue(row["chat_missing"])
        self.assertNotIn("ended", row["status"])
        chat_file = self.repo / ".cptr" / "chats" / f"{task['chat_id']}.json"
        chat_file.unlink(missing_ok=True)
        await self.client.get(f"/api/coordination/tasks?root={self.repo}")
        self.assertFalse(chat_file.exists())  # no backfill for a deleted chat
        response = await self.client.request(
            "DELETE", f"/api/coordination/tasks/{task['id']}", json={"confirm": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(await CoordTask.get_by_id(task["id"]))


class LimitsTests(ApiTestCase):
    async def test_default_is_four_and_admin_can_change_it(self):
        limits = (await self.client.get("/api/coordination/limits")).json()
        self.assertEqual(limits["max_writers"], 4)
        response = await self.client.put("/api/coordination/limits", json={"max_writers": 6})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["max_writers"], 6)
        listed = (await self.client.get(f"/api/coordination/tasks?root={self.repo}")).json()
        self.assertEqual(listed["limits"]["max_writers"], 6)

    async def test_non_admin_cannot_change_it_and_range_is_checked(self):
        bad = await self.client.put("/api/coordination/limits", json={"max_writers": 0})
        self.assertEqual(bad.status_code, 422)
        self.client.cookies.set("cptr_session", create_token("someone-else", "other", "user"))
        response = await self.client.put("/api/coordination/limits", json={"max_writers": 8})
        self.assertEqual(response.status_code, 403)


class UsageTests(ApiTestCase):
    async def test_usage_sums_task_chats_and_totals(self):
        from cptr.models.chats import ChatMessage

        body = {"root": str(self.repo), "create_chat": True, "model_id": "m"}
        a = (await self.post("/tasks", {**body, "title": "Usage A"})).json()["task"]
        b = (await self.post("/tasks", {**body, "title": "Usage B"})).json()["task"]
        call = {"type": "function_call", "name": "run_command", "arguments": "{}"}
        await ChatMessage.create(a["chat_id"], "user", "go")
        await ChatMessage.create(
            a["chat_id"],
            "assistant",
            "done",
            output=[call, call],
            usage={"input_tokens": 100, "output_tokens": 20, "duration_ms": 3000},
        )
        await ChatMessage.create(
            b["chat_id"],
            "assistant",
            "ok",
            usage={"input_tokens": 10, "output_tokens": 5},
        )
        response = await self.client.get(f"/api/coordination/usage?root={self.repo}")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        ua = data["tasks"][a["id"]]
        self.assertEqual((ua["tokens"], ua["tool_calls"], ua["turns"]), (120, 2, 1))
        self.assertEqual(ua["duration_ms"], 3000)
        self.assertEqual(data["tasks"][b["id"]]["tokens"], 15)
        self.assertEqual(data["totals"]["tokens"], 135)
        self.assertEqual(data["totals"]["tool_calls"], 2)

    async def test_usage_hides_other_users_tasks(self):
        await self.post("/tasks", {"root": str(self.repo), "title": "Private"})
        self.client.cookies.set("cptr_session", create_token("someone-else", "other", "user"))
        data = (await self.client.get(f"/api/coordination/usage?root={self.repo}")).json()
        self.assertEqual((data["tasks"], data["totals"]["tokens"]), ({}, 0))


class UsageCacheTests(ApiTestCase):
    """Usage is tallied once per chat and reused until the chat changes."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from cptr.routers import coordination as coord_router

        coord_router._chat_usage_cache.clear()
        coord_router._message_usage_cache.clear()

    async def _usage(self, task_id: str) -> dict:
        data = (await self.client.get(f"/api/coordination/usage?root={self.repo}")).json()
        return data["tasks"][task_id]

    async def test_cached_until_the_chat_is_touched_and_subagents_count(self):
        from unittest import mock

        from cptr.models.chats import Chat, ChatMessage
        from cptr.routers import chat as chat_router
        from cptr.utils.config import now_ms
        from tests.test_coordination import USER as U

        body = {"root": str(self.repo), "create_chat": True, "model_id": "m", "title": "Cached"}
        task = (await self.post("/tasks", body)).json()["task"]
        await ChatMessage.create(task["chat_id"], "assistant", "a", usage={"input_tokens": 100})
        sub = await Chat.create(
            U, "sub", meta={"subagent": True, "type": "subagent", "parent_chat_id": task["chat_id"]}
        )
        await ChatMessage.create(sub.id, "assistant", "s", usage={"input_tokens": 7})
        self.assertEqual((await self._usage(task["id"]))["tokens"], 107)

        real = chat_router._tally_messages
        with mock.patch.object(chat_router, "_tally_messages", side_effect=real) as tally:
            self.assertEqual((await self._usage(task["id"]))["tokens"], 107)
            self.assertEqual(tally.call_count, 0)  # nothing changed: all from cache

            await ChatMessage.create(task["chat_id"], "assistant", "b", usage={"input_tokens": 3})
            await Chat.touch(task["chat_id"], now_ms() + 1)  # as at the end of a turn
            self.assertEqual((await self._usage(task["id"]))["tokens"], 110)
            self.assertEqual(tally.call_count, 1)  # only the changed chat is re-read

    async def test_a_running_chat_is_never_cached(self):
        from unittest import mock

        from cptr.models.chats import ChatMessage
        from cptr.routers import coordination as coord_router

        body = {"root": str(self.repo), "create_chat": True, "model_id": "m", "title": "Live"}
        task = (await self.post("/tasks", body)).json()["task"]
        await ChatMessage.create(task["chat_id"], "assistant", "a", usage={"input_tokens": 5})
        with mock.patch("cptr.utils.chat_task.get_active_chat_ids", return_value={task["chat_id"]}):
            self.assertEqual((await self._usage(task["id"]))["tokens"], 5)
            await ChatMessage.create(task["chat_id"], "assistant", "b", usage={"input_tokens": 4})
            self.assertEqual((await self._usage(task["id"]))["tokens"], 9)
        self.assertNotIn(task["chat_id"], {k[0] for k in coord_router._chat_usage_cache})

    async def test_a_running_chat_rereads_only_replies_that_changed(self):
        from unittest import mock

        from cptr.models.chats import ChatMessage
        from cptr.routers import chat as chat_router

        body = {"root": str(self.repo), "create_chat": True, "model_id": "m", "title": "Busy"}
        task = (await self.post("/tasks", body)).json()["task"]
        chat_id = task["chat_id"]
        for i in range(5):  # finished replies with big tool outputs
            await ChatMessage.create(
                chat_id,
                "assistant",
                f"r{i}",
                output=[{"type": "function_call", "call_id": f"c{i}", "name": "x"}],
                usage={"input_tokens": 10},
            )
        live = await ChatMessage.create(
            chat_id, "assistant", "", done=False, usage={"input_tokens": 1}
        )
        real = chat_router._tally_messages
        with (
            mock.patch("cptr.utils.chat_task.get_active_chat_ids", return_value={chat_id}),
            mock.patch.object(chat_router, "_tally_messages", side_effect=real) as tally,
        ):
            first = await self._usage(task["id"])
            self.assertEqual((first["tokens"], first["tool_calls"], first["turns"]), (51, 5, 6))
            self.assertEqual(tally.call_count, 6)

            tally.reset_mock()
            await ChatMessage.update(live.id, content="streaming…", usage={"input_tokens": 4})
            again = await self._usage(task["id"])
            self.assertEqual(again["tokens"], 54)
            self.assertEqual(tally.call_count, 1)  # only the reply still being written

            tally.reset_mock()
            first_reply = (await ChatMessage.get_all_by_chat(chat_id))[0]
            await ChatMessage.update(first_reply.id, content="edited and longer")
            await self._usage(task["id"])
            self.assertEqual(tally.call_count, 2)  # the edited reply and the live one


class OriginFolderTests(ApiTestCase):
    """A task belongs to the folder it was started from, even in a shared repo."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import os

        self.side = Path(os.path.realpath(self.repo.parent / "side-folder"))
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(self.side))

    def _file(self, folder: Path, chat_id: str) -> Path:
        return folder / ".cptr" / "chats" / f"{chat_id}.json"

    async def test_task_from_a_worktree_is_filed_under_that_worktree(self):
        body = {"root": str(self.side), "title": "Side", "create_chat": True, "model_id": "m"}
        task = (await self.post("/tasks", body)).json()["task"]
        self.assertEqual(task["meta"]["origin_root"], str(self.side))
        self.assertTrue(self._file(self.side, task["chat_id"]).exists())
        self.assertFalse(self._file(self.repo, task["chat_id"]).exists())

        listed = (await self.client.get(f"/api/coordination/tasks?root={self.repo}")).json()
        self.assertEqual(listed["root"], str(self.repo))  # one shared list, origin per task
        self.assertEqual([t["meta"]["origin_root"] for t in listed["tasks"]], [str(self.side)])

    async def test_older_task_is_moved_to_the_folder_on_its_base_branch(self):
        from cptr.routers import coordination as coord_router

        body = {"root": str(self.repo), "title": "Old", "create_chat": True, "model_id": "m"}
        task = (await self.post("/tasks", body)).json()["task"]
        # As if created before origins were recorded, from the "side" worktree.
        meta = {**task["meta"], "base_branch": "side"}
        meta.pop("origin_root")
        await CoordTask.update_fields(task["id"], meta=meta)
        coord_router._chat_files_ok.clear()

        await self.client.get(f"/api/coordination/tasks?root={self.repo}")
        self.assertEqual(
            (await CoordTask.get_by_id(task["id"])).meta["origin_root"], str(self.side)
        )
        self.assertEqual((await Chat.get_by_id(task["chat_id"])).meta["workspace"], str(self.side))
        self.assertTrue(self._file(self.side, task["chat_id"]).exists())
        self.assertFalse(self._file(self.repo, task["chat_id"]).exists())


class SummaryTests(ApiTestCase):
    """Home's summary lists task agents from every folder of every repository."""

    async def test_lists_tasks_from_every_folder_and_drops_old_finished_ones(self):
        import os

        from sqlalchemy import update

        from cptr.models.coordination import CoordTask
        from cptr.utils.db import get_db

        side = Path(os.path.realpath(self.repo.parent / "side-folder"))
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(side))
        await self.post("/tasks", {"root": str(self.repo), "title": "Here"})
        await self.post("/tasks", {"root": str(side), "title": "Side"})
        old = (await self.post("/tasks", {"root": str(self.repo), "title": "Old"})).json()
        async with await get_db() as db:  # ended long ago
            await db.execute(
                update(CoordTask)
                .where(CoordTask.id == old["task"]["id"])
                .values(status="ended", updated_at=1)
            )
            await db.commit()

        data = (await self.client.get("/api/coordination/summary")).json()
        origins = {t["title"]: t["meta"]["origin_root"] for t in data["tasks"]}
        self.assertEqual(sorted(origins), ["Here", "Side"])
        self.assertEqual(origins["Side"], str(side))
        self.assertIn("max_writers", data["limits"])
        self.assertEqual(data["active_chats"], [])
        self.assertEqual(data["dependencies"], [])

    async def test_usage_adds_per_task_usage_and_conflicts(self):
        task = (await self.post("/tasks", {"root": str(self.repo), "title": "Counted"})).json()
        plain = (await self.client.get("/api/coordination/summary")).json()
        self.assertNotIn("usage", plain)
        data = (await self.client.get("/api/coordination/summary?usage=true")).json()
        tid = task["task"]["id"]
        self.assertEqual(set(data["usage"]["tasks"]), {tid})
        self.assertEqual(data["usage"]["tasks"][tid]["tokens"], 0)
        self.assertEqual(data["usage"]["totals"]["turns"], 0)
        self.assertEqual(data["conflicts"], {task["task"]["repo_key"]: 0})


class MarkDoneRouteTests(ApiTestCase):
    """Mark done through the API, from each state a dependency is likely in."""

    async def _pair(self):
        from cptr.models.chats import Chat
        from tests.test_coordination import USER as U

        a_chat = await Chat.create(U, "A", meta={"last_model": "m"})
        a = await self.task("Dep", chat_id=a_chat.id, start_prompt="do it")
        b_chat = await Chat.create(U, "B", meta={"last_model": "m"})
        b = await self.task("Waits", chat_id=b_chat.id, start_prompt="go", depends_on=[a["id"]])
        await coordinator.refresh(a["repo_key"])
        return a, b

    async def _assert_dependent_starts(self, a, b):
        from cptr.models.coordination import CoordConflict, CoordTask

        await coordinator.refresh(a["repo_key"])
        await coordinator.wait_idle()
        dep = await CoordTask.get_by_id(a["id"])
        task = await CoordTask.get_by_id(b["id"])
        open_kinds = [
            c.kind
            for c in await CoordConflict.query(a["repo_key"], statuses={"open", "acknowledged"})
        ]
        self.assertEqual(dep.status, "completed")
        self.assertNotIn("dependency_failed", open_kinds)
        self.assertEqual(task.blockers, [])
        self.assertIn(b["id"], [t for t, _ in self.launched])

    async def test_done_while_its_agent_is_running(self):
        from cptr.models.coordination import CoordTask

        a, b = await self._pair()
        self.assertEqual((await CoordTask.get_by_id(a["id"])).status, "running")
        r = await self.post(f"/tasks/{a['id']}/done")
        self.assertEqual(r.status_code, 200, r.text)
        await self._assert_dependent_starts(a, b)

    async def test_done_from_review(self):
        from cptr.utils.coordination import registry

        a, b = await self._pair()
        await registry.set_status(a["id"], "reviewing")
        r = await self.post(f"/tasks/{a['id']}/done")
        self.assertEqual(r.status_code, 200, r.text)
        await self._assert_dependent_starts(a, b)

    async def test_done_after_end(self):
        a, b = await self._pair()
        await self.post(f"/tasks/{a['id']}/end")
        await coordinator.refresh(a["repo_key"])
        r = await self.post(f"/tasks/{a['id']}/done")
        self.assertEqual(r.status_code, 200, r.text)
        await self._assert_dependent_starts(a, b)

    async def test_a_late_turn_end_does_not_undo_done(self):
        """The stopped turn's end (and a failed-turn event) arrive after Mark done."""
        from cptr.models import ChatMessage
        from cptr.models.coordination import CoordTask
        from cptr.utils.config import now_ms

        a, b = await self._pair()
        task = await CoordTask.get_by_id(a["id"])
        self.assertEqual(task.status, "running")
        r = await self.post(f"/tasks/{a['id']}/done")
        self.assertEqual(r.status_code, 200, r.text)
        # What the interrupted turn leaves behind, then its end-of-turn hook.
        await ChatMessage.create(
            chat_id=task.chat_id,
            role="assistant",
            content="CHECKOUT: blocked - interrupted",
            done=True,
            created_at=now_ms(),
        )
        self.assertIsNone(await coordinator.on_turn_end(task.chat_id, grace=0))

        class _Ev:
            event = "chat.failed"
            subject = {"id": task.chat_id}

        await coordinator.CoordinationEventSink().handle_event(_Ev())
        await self._assert_dependent_starts(a, b)


class NewTaskChatSettingsTests(ApiTestCase):
    """New task's Plan mode / Follow edits land where the message box keeps them."""

    async def test_plan_mode_and_follow_edits_are_saved_on_the_new_chat(self):
        from cptr.models.chats import Chat

        body = {"root": str(self.repo), "title": "Planned", "create_chat": True, "model_id": "m"}
        task = (await self.post("/tasks", {**body, "plan_mode": True, "follow_edits": True})).json()
        chat = await Chat.get_by_id(task["task"]["chat_id"])
        self.assertEqual(chat.meta["params"], {"plan_mode": True, "follow_edits": True})
        self.assertEqual(chat.meta["last_model"], "m")

    async def test_off_by_default_and_nothing_extra_is_saved(self):
        from cptr.models.chats import Chat

        body = {"root": str(self.repo), "title": "Plain", "create_chat": True, "model_id": "m"}
        task = (await self.post("/tasks", body)).json()
        chat = await Chat.get_by_id(task["task"]["chat_id"])
        self.assertNotIn("params", chat.meta)
