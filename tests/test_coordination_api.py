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
