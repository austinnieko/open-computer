"""OpenCode 1.x turns against a mock 1.x server: quiet agents, dropped and
half-open event streams, recovery without repeating work.

Run: uv run python -m unittest tests.test_opencode_v1 -v

The mock follows the OpenCode 1.19.5 contract this adapter relies on
(``/event`` SSE with ``server.heartbeat`` every few seconds, ``/session/...``
routes, bus events ``message.updated`` / ``message.part.updated`` /
``message.part.delta`` / ``session.status`` / ``session.error`` /
``permission.asked``). Timings are shortened. It is a mock: a real OpenCode
and a real model were not involved.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import unittest
from typing import Any
from unittest import mock

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from cptr.utils.agents import opencode_v1
from cptr.utils.agents.attachments import PreparedAgentAttachments
from cptr.utils.agents.events import (
    AgentDone,
    AgentError,
    AgentReasoningDelta,
    AgentStatus,
    AgentTextDelta,
    AgentToolUpdate,
)
from cptr.utils.agents.opencode import run_opencode_agent


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MockOpenCode1:
    """Enough of OpenCode 1.19.5 for the adapter, with fault injection."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.reset()
        self.app = self._build()

    def reset(self) -> None:
        self.session_id = f"ses_{time.time_ns()}"
        self.sessions: set[str] = set()
        self.history: list[dict[str, Any]] = []
        self.status: dict[str, Any] = {}
        self.script: list[tuple] = []
        self.prompts: list[tuple[str, dict]] = []
        self.aborts = 0
        self.subscribers: list[asyncio.Queue] = []
        self.muted = False
        self.status_fails = False
        self.events_fail = False
        self.prompt_status = 204
        self.subscribe_count = 0
        self.heartbeat = 0.1
        self.busy_until = 0.0  # a previous turn still running (wall clock)
        self.prompt_times: list[float] = []

    # history mirrors what the real server persists
    def _record(self, event: dict[str, Any]) -> None:
        props = event.get("properties", {})
        if event["type"] == "message.updated":
            info = props["info"]
            for m in self.history:
                if m["info"]["id"] == info["id"]:
                    m["info"] = info
                    return
            self.history.append({"info": info, "parts": []})
        elif event["type"] == "message.part.updated":
            part = props["part"]
            for m in self.history:
                if m["info"]["id"] == part["messageID"]:
                    for i, p in enumerate(m["parts"]):
                        if p["id"] == part["id"]:
                            m["parts"][i] = part
                            return
                    m["parts"].append(part)

    async def publish(self, event: dict[str, Any]) -> None:
        event = json.loads(json.dumps(event).replace("$S", self.session_id))
        self._record(event)
        if self.muted:
            return
        for q in list(self.subscribers):
            await q.put(event)

    async def drop_streams(self) -> None:
        for q in list(self.subscribers):
            await q.put(None)

    async def _run(self) -> None:
        # The adapter subscribes before prompting; on a cold mock the first
        # connection can land a moment after the prompt.
        for _ in range(200):
            if self.subscribers:
                break
            await asyncio.sleep(0.01)
        for step in self.script:
            kind = step[0]
            if kind == "event":
                await self.publish(step[1])
            elif kind == "sleep":
                await asyncio.sleep(step[1])
            elif kind == "drop":
                await self.drop_streams()
            elif kind == "mute":
                self.muted = step[1]
            elif kind == "status":
                if step[1] == "idle":
                    self.status.pop(self.session_id, None)
                else:
                    self.status[self.session_id] = {"type": step[1], **(step[2] if len(step) > 2 else {})}
                full = {"type": step[1], **(step[2] if len(step) > 2 else {})}
                await self.publish(
                    {"type": "session.status", "properties": {"sessionID": "$S", "status": full}}
                )
            elif kind == "fail_status":
                self.status_fails = step[1]
            elif kind == "fail_events":
                self.events_fail = step[1]
            await asyncio.sleep(0.01)

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.post("/session")
        async def create():
            self.sessions.add(self.session_id)
            return {"id": self.session_id}

        @app.get("/session/status")
        async def status():
            if self.status_fails:
                return JSONResponse({"message": "boom"}, status_code=503)
            if time.time() < self.busy_until:
                return {**self.status, self.session_id: {"type": "busy"}}
            return self.status

        @app.get("/session/{sid}")
        async def get_session(sid: str):
            if sid not in self.sessions:
                return JSONResponse({"name": "NotFoundError", "data": {"message": "no"}}, 404)
            return {"id": sid}

        @app.get("/session/{sid}/message")
        async def messages(sid: str, limit: int = 100):
            return self.history[-limit:]

        @app.post("/session/{sid}/prompt_async")
        async def prompt(sid: str, request: Request):
            body = await request.json()
            self.prompts.append((sid, body))
            self.prompt_times.append(time.time())
            if self.prompt_status != 204:
                return JSONResponse(
                    {"name": "BadRequest", "data": {"message": "model not found"}},
                    status_code=self.prompt_status,
                )
            self.status[sid] = {"type": "busy"}
            asyncio.create_task(self._run())
            return Response(status_code=204)

        @app.post("/session/{sid}/message")
        async def blocking_prompt(sid: str):
            raise AssertionError("the blocking /message route must never be used")

        @app.post("/session/{sid}/abort")
        async def abort(sid: str):
            self.aborts += 1
            self.status.pop(sid, None)
            return True

        @app.get("/event")
        async def events():
            if self.events_fail:
                return JSONResponse({"message": "down"}, status_code=503)
            self.subscribe_count += 1
            q: asyncio.Queue = asyncio.Queue()
            self.subscribers.append(q)

            async def stream():
                yield f"data: {json.dumps({'type': 'server.connected', 'properties': {}})}\n\n"
                try:
                    while True:
                        try:
                            event = await asyncio.wait_for(q.get(), timeout=self.heartbeat)
                        except asyncio.TimeoutError:
                            if not self.muted:
                                yield 'data: {"type":"server.heartbeat","properties":{}}\n\n'
                            continue
                        if event is None:
                            return
                        yield f"data: {json.dumps(event)}\n\n"
                finally:
                    self.subscribers.remove(q)

            return StreamingResponse(stream(), media_type="text/event-stream")

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def web_ui(path: str):
            return HTMLResponse("<!doctype html><html>opencode</html>")

        return app

    def start(self) -> None:
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def user(mid="u1"):
    return (
        "event",
        {
            "type": "message.updated",
            "properties": {
                "sessionID": "$S",
                "info": {"id": mid, "role": "user", "time": {"created": time.time() * 1000}},
            },
        },
    )


def assistant(mid="a1", parent="u1"):
    return (
        "event",
        {
            "type": "message.updated",
            "properties": {
                "sessionID": "$S",
                "info": {
                    "id": mid,
                    "role": "assistant",
                    "parentID": parent,
                    "time": {"created": time.time() * 1000},
                },
            },
        },
    )


def part(pid, kind, text=None, mid="a1", **extra):
    p = {"id": pid, "messageID": mid, "sessionID": "$S", "type": kind, **extra}
    if text is not None:
        p["text"] = text
    return ("event", {"type": "message.part.updated", "properties": {"sessionID": "$S", "part": p}})


def delta(pid, text, mid="a1"):
    return (
        "event",
        {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "$S",
                "messageID": mid,
                "partID": pid,
                "field": "text",
                "delta": text,
            },
        },
    )


def tool(call, status, **state):
    return part(
        f"p_{call}",
        "tool",
        callID=call,
        tool="bash",
        state={"status": status, "input": {"command": "npm test"}, **state},
    )


FAST = {
    "STREAM_STALE_SECONDS": 1.0,
    "STATUS_POLL_SECONDS": 0.2,
    "STATUS_REPORT_SECONDS": 0.4,
    "WAITING_AFTER_SECONDS": 0.5,
    "START_GRACE_SECONDS": 0.5,
    "RECONNECT_DELAYS": (0.3, 0.3, 0.3),
    "MAX_RECONNECTS": 3,
    "UNREACHABLE_SECONDS": 1.5,
    "SUBSCRIBE_WAIT_SECONDS": 2.0,
}


class OpenCode1Tests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mock = MockOpenCode1()
        cls.mock.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mock.stop()

    def setUp(self) -> None:
        self.mock.reset()
        patcher = mock.patch.multiple(opencode_v1, **FAST)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def run_turn(self, resume_state=None, timeout=20):
        events = []

        async def go():
            async for event in run_opencode_agent(
                profile={"id": "oc", "agent": "opencode", "server_url": self.mock.url},
                model="glm/glm-4.6",
                workspace="/tmp/repo",
                messages=[{"role": "user", "content": "fix the bug"}],
                system_prompt="",
                chat_params={},
                resume_state=resume_state,
                attachments=PreparedAgentAttachments(images=[], files=[], prompt_suffix=""),
            ):
                events.append(event)

        await asyncio.wait_for(go(), timeout=timeout)
        return events

    @staticmethod
    def text(events):
        return "".join(e.text for e in events if isinstance(e, AgentTextDelta))

    @staticmethod
    def reasoning(events):
        return "".join(e.text for e in events if isinstance(e, AgentReasoningDelta))

    async def test_normal_turn_separates_reasoning_text_and_tools(self):
        self.mock.script = [
            user(),
            assistant(),
            part("r1", "reasoning", ""),
            delta("r1", "Looking at "),
            delta("r1", "the test."),
            tool("c1", "running"),
            tool("c1", "completed", output="ok"),
            part("t1", "text", ""),
            delta("t1", "Fixed "),
            part("t1", "text", "Fixed it."),
            ("status", "idle"),
        ]
        events = await self.run_turn()
        self.assertEqual(self.reasoning(events), "Looking at the test.")
        self.assertEqual(self.text(events), "Fixed it.")
        tools = [e for e in events if isinstance(e, AgentToolUpdate)]
        self.assertEqual([t.status for t in tools], ["in_progress", "completed"])
        self.assertIsInstance(events[-1], AgentDone)
        self.assertEqual(events[-1].resume_state["session_id"], self.mock.session_id)
        self.assertEqual(len(self.mock.prompts), 1)

    async def test_long_silent_tool_is_waiting_not_failed(self):
        self.mock.script = [
            user(),
            assistant(),
            tool("c1", "running"),
            # Several times the "waiting" threshold with only heartbeats.
            ("sleep", 2.5),
            tool("c1", "completed", output="built"),
            part("t1", "text", "Build passed."),
            ("status", "idle"),
        ]
        events = await self.run_turn()
        self.assertFalse([e for e in events if isinstance(e, AgentError)])
        states = [e.state for e in events if isinstance(e, AgentStatus)]
        self.assertIn("waiting", states)
        self.assertNotIn("reconnecting", states)
        waiting = next(e for e in events if isinstance(e, AgentStatus) and e.state == "waiting")
        self.assertIsNotNone(waiting.last_activity)
        self.assertEqual(self.text(events), "Build passed.")
        self.assertEqual(self.mock.subscribe_count, 1)

    async def test_dropped_stream_reconnects_and_replays_missed_output_once(self):
        self.mock.script = [
            user(),
            assistant(),
            part("t1", "text", "Part one. "),
            ("drop",),
            # Produced while the adapter is disconnected: only in history.
            ("sleep", 0.05),
            tool("c1", "running"),
            tool("c1", "completed", output="ok"),
            part("t1", "text", "Part one. Part two."),
            ("sleep", 0.6),
            part("t2", "text", " Done."),
            ("status", "idle"),
        ]
        events = await self.run_turn()
        self.assertEqual(self.text(events), "Part one. Part two. Done.")
        self.assertIn("reconnecting", [e.state for e in events if isinstance(e, AgentStatus)])
        self.assertIn("c1", {e.call_id for e in events if isinstance(e, AgentToolUpdate)})
        self.assertGreaterEqual(self.mock.subscribe_count, 2)
        self.assertEqual(len(self.mock.prompts), 1)  # never re-sent
        self.assertIsInstance(events[-1], AgentDone)

    async def test_half_open_stream_without_heartbeats_is_replaced(self):
        self.mock.script = [
            user(),
            assistant(),
            ("mute", True),  # connection stays open, nothing arrives
            part("t1", "text", "Recovered text."),
            ("sleep", 1.6),
            ("mute", False),
            ("sleep", 0.6),
            ("status", "idle"),
        ]
        events = await self.run_turn()
        self.assertEqual(self.text(events), "Recovered text.")
        self.assertGreaterEqual(self.mock.subscribe_count, 2)
        self.assertEqual(len(self.mock.prompts), 1)

    async def test_finishes_from_status_when_idle_event_is_missed(self):
        self.mock.script = [
            user(),
            assistant(),
            part("t1", "text", "All done."),
            ("mute", True),
            ("status", "idle"),  # published while muted: never delivered
            ("mute", False),
        ]
        events = await self.run_turn()
        self.assertEqual(self.text(events), "All done.")
        self.assertIsInstance(events[-1], AgentDone)

    async def test_unreachable_server_fails_clearly_and_keeps_the_session(self):
        self.mock.script = [
            user(),
            assistant(),
            part("t1", "text", "Started."),
            ("fail_events", True),
            ("fail_status", True),
            ("drop",),
            ("sleep", 30),
        ]
        events = await self.run_turn()
        error = events[-1]
        self.assertIsInstance(error, AgentError)
        self.assertRegex(error.message, r"not answered|could not reconnect")
        self.assertEqual(error.resume_state["session_id"], self.mock.session_id)
        self.assertEqual(self.text(events), "Started.")
        self.assertEqual(len(self.mock.prompts), 1)

    async def test_session_error_is_reported(self):
        self.mock.script = [
            user(),
            assistant(),
            (
                "event",
                {
                    "type": "session.error",
                    "properties": {
                        "sessionID": "$S",
                        "error": {"name": "APIError", "data": {"message": "GLM returned 503"}},
                    },
                },
            ),
            ("status", "idle"),
        ]
        events = await self.run_turn()
        self.assertIsInstance(events[-1], AgentError)
        self.assertIn("GLM returned 503", events[-1].message)

    async def test_retrying_model_is_reported(self):
        self.mock.script = [
            user(),
            assistant(),
            ("status", "retry", {"attempt": 2, "message": "rate limited"}),
            ("sleep", 0.8),
            ("status", "busy"),
            part("t1", "text", "ok"),
            ("status", "idle"),
        ]
        events = await self.run_turn()
        retry = [e for e in events if isinstance(e, AgentStatus) and e.state == "retrying"]
        self.assertTrue(retry)
        self.assertEqual(retry[0].detail, "rate limited")

    async def test_permission_request_is_shown(self):
        self.mock.script = [
            user(),
            assistant(),
            (
                "event",
                {
                    "type": "permission.asked",
                    "properties": {
                        "id": "per_1",
                        "sessionID": "$S",
                        "permission": "bash",
                        "patterns": ["rm -rf build"],
                        "metadata": {},
                        "always": [],
                    },
                },
            ),
            ("status", "idle"),
        ]
        events = await self.run_turn()
        self.assertIn("Waiting for permission: `bash`", self.text(events))

    async def test_resume_reuses_session_and_missing_one_starts_fresh(self):
        self.mock.sessions.add(self.mock.session_id)
        self.mock.script = [user(), assistant(), part("t1", "text", "hi"), ("status", "idle")]
        events = await self.run_turn(resume_state={"session_id": self.mock.session_id})
        self.assertEqual(self.mock.prompts[0][0], self.mock.session_id)
        self.assertIsInstance(events[-1], AgentDone)

        self.mock.reset()
        self.mock.script = [user(), assistant(), part("t1", "text", "hi"), ("status", "idle")]
        events = await self.run_turn(resume_state={"session_id": "ses_gone"})
        self.assertEqual(self.mock.prompts[0][0], self.mock.session_id)
        self.assertIsInstance(events[-1], AgentDone)

    async def test_new_session_is_saved_before_any_work(self):
        self.mock.script = [user(), assistant(), part("t1", "text", "hi"), ("status", "idle")]
        events = await self.run_turn()
        from cptr.utils.agents.events import AgentSessionStarted

        self.assertIsInstance(events[0], AgentSessionStarted)
        self.assertEqual(events[0].resume_state["session_id"], self.mock.session_id)

        self.mock.reset()
        self.mock.sessions.add(self.mock.session_id)
        self.mock.script = [user(), assistant(), part("t1", "text", "hi"), ("status", "idle")]
        events = await self.run_turn(resume_state={"session_id": self.mock.session_id})
        self.assertFalse([e for e in events if isinstance(e, AgentSessionStarted)])

    async def test_resumed_session_still_busy_is_followed_not_duplicated(self):
        # e.g. Open Computer restarted while OpenCode kept working.
        self.mock.sessions.add(self.mock.session_id)
        self.mock.busy_until = time.time() + 1.0
        self.mock.script = [user(), assistant(), part("t1", "text", "Next answer."), ("status", "idle")]
        events = await self.run_turn(resume_state={"session_id": self.mock.session_id})
        self.assertIn("still working on the previous request", self.text(events))
        self.assertEqual(len(self.mock.prompts), 1)
        self.assertGreaterEqual(self.mock.prompt_times[0], self.mock.busy_until)
        self.assertTrue(self.text(events).endswith("Next answer."))
        self.assertIsInstance(events[-1], AgentDone)

    async def test_rejected_prompt_is_reported_not_retried_elsewhere(self):
        self.mock.sessions.add(self.mock.session_id)
        self.mock.prompt_status = 400
        events = await self.run_turn(resume_state={"session_id": self.mock.session_id})
        self.assertIsInstance(events[-1], AgentError)
        self.assertIn("HTTP 400", events[-1].message)
        self.assertIn("model not found", events[-1].message)
        self.assertNotIn("Authorization", events[-1].message)
        self.assertEqual(len(self.mock.prompts), 1)  # no second session, no /message fallback
        self.assertEqual(self.mock.sessions, {self.mock.session_id})


class TranslatorTests(unittest.TestCase):
    def test_replay_after_live_events_emits_only_the_missing_suffix(self):
        tr = opencode_v1.Translator("s")
        tr.sent_ms = 0
        tr.translate(
            {
                "type": "message.updated",
                "properties": {"sessionID": "s", "info": {"id": "u", "role": "user"}},
            }
        )
        tr.translate(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "s",
                    "part": {"id": "p", "messageID": "a", "type": "text", "text": "Hello"},
                },
            }
        )
        history = [
            {"info": {"id": "old", "role": "assistant", "parentID": "earlier"}, "parts": [
                {"id": "x", "messageID": "old", "type": "text", "text": "previous turn"}
            ]},
            {"info": {"id": "u", "role": "user"}, "parts": [
                {"id": "q", "messageID": "u", "type": "text", "text": "fix the bug"}
            ]},
            {"info": {"id": "a", "role": "assistant", "parentID": "u"}, "parts": [
                {"id": "p", "messageID": "a", "type": "text", "text": "Hello world"}
            ]},
        ]
        out = tr.replay(history)
        self.assertEqual([e.text for e in out], [" world"])
        self.assertEqual(tr.replay(history), [])  # idempotent


if __name__ == "__main__":
    unittest.main()
