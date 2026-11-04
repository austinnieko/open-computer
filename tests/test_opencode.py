"""OpenCode adapter tests against a mock OpenCode 2.x server.

Run: uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from cptr.utils.agents import opencode_v2
from cptr.utils.agents.attachments import PreparedAgentAttachments
from cptr.utils.agents.detection import _probe_opencode_models
from cptr.utils.agents.events import (
    AgentDone,
    AgentError,
    AgentReasoningDelta,
    AgentTextDelta,
    AgentToolUpdate,
)
from cptr.utils.agents.opencode import resolve_opencode_password, run_opencode_agent

PASSWORD = "test-password"
SESSION = "ses_test1"
WEB_UI = "<!doctype html><html><body>opencode</body></html>"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _event(event_type: str, **data: Any) -> dict[str, Any]:
    return {"id": f"evt_{time.time_ns()}", "created": time.time(), "type": event_type, "data": data}


class MockOpenCode:
    """Just enough of the OpenCode 2.2.5 HTTP contract for the adapter."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.sessions: set[str] = set()
        self.script: list[dict[str, Any]] = []
        self.active = False
        self.hold_open = False  # keep the run going until interrupted
        self.subscribers: list[asyncio.Queue] = []
        self.tasks: set[asyncio.Task] = set()
        self.session_id = SESSION
        self.app = self._build()
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"

    def _authorized(self, request: Request) -> bool:
        expected = base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        return request.headers.get("authorization") == f"Basic {expected}"

    async def _publish(self, envelope: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            await queue.put(envelope)

    def reset(self) -> None:
        """Fresh state per test: unique session id, no scripts left running."""
        for task in list(self.tasks):
            task.get_loop().call_soon_threadsafe(task.cancel)
        self.tasks.clear()
        self.requests.clear()
        self.sessions.clear()
        self.session_id = f"ses_{time.time_ns()}"
        self.active = False
        self.hold_open = False

    async def _run_script(self, session_id: str) -> None:
        self.active = True
        await asyncio.sleep(0.05)
        for envelope in self.script:
            envelope = json.loads(json.dumps(envelope).replace("$SESSION", session_id))
            await self._publish(envelope)
            await asyncio.sleep(0.01)
        if self.hold_open:
            return
        self.active = False

    def _build(self) -> FastAPI:
        app = FastAPI()
        unauthorized = JSONResponse(
            {"_tag": "UnauthorizedError", "message": "Unauthorized"}, status_code=401
        )

        @app.middleware("http")
        async def auth(request: Request, call_next):
            if request.url.path.startswith("/api/"):
                self.requests.append((request.method, request.url.path, None))
                if not self._authorized(request):
                    return unauthorized
            return await call_next(request)

        @app.get("/api/session/active")
        async def active():
            return {"data": {self.session_id: {"type": "running"}} if self.active else {}}

        @app.get("/api/model")
        async def models():
            return {
                "location": {"directory": "/"},
                "data": [
                    {"id": "qwen3", "modelID": "Qwen/Qwen3", "providerID": "spark"},
                    {"id": "off", "modelID": "off", "providerID": "spark", "enabled": False},
                ],
            }

        @app.post("/api/session")
        async def create(request: Request):
            body = await request.json()
            self.requests[-1] = ("POST", "/api/session", body)
            self.sessions.add(self.session_id)
            return {"data": {"id": self.session_id}}

        @app.get("/api/session/{session_id}")
        async def get_session(session_id: str):
            if session_id not in self.sessions:
                return JSONResponse(
                    {"_tag": "SessionNotFoundError", "sessionID": session_id, "message": "nope"},
                    status_code=404,
                )
            return {"data": {"id": session_id}}

        @app.post("/api/session/{session_id}/model")
        async def switch(session_id: str, request: Request):
            self.requests[-1] = ("POST", f"/api/session/{session_id}/model", await request.json())
            return {"data": {}}

        @app.post("/api/session/{session_id}/prompt")
        async def prompt(session_id: str, request: Request):
            body = await request.json()
            self.requests[-1] = ("POST", f"/api/session/{session_id}/prompt", body)
            task = asyncio.create_task(self._run_script(session_id))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return {"data": {"id": "msg_1", "sessionID": session_id, "type": "user"}}

        @app.post("/api/session/{session_id}/interrupt")
        async def interrupt(session_id: str):
            self.active = False
            await self._publish(
                _event("session.execution.interrupted", sessionID=session_id, reason="user")
            )
            return {"interrupted": True}

        @app.get("/api/event")
        async def events():
            queue: asyncio.Queue = asyncio.Queue()
            self.subscribers.append(queue)

            async def stream():
                yield f"data: {json.dumps(_event('server.connected'))}\n\n"
                try:
                    while True:
                        envelope = await queue.get()
                        yield f"data: {json.dumps(envelope)}\n\n"
                finally:
                    self.subscribers.remove(queue)

            return StreamingResponse(stream(), media_type="text/event-stream")

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def web_ui(path: str):
            # OpenCode serves its web UI, with 200, for any unknown route.
            return HTMLResponse(WEB_UI)

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


def _profile(url: str, password: str | None = PASSWORD) -> dict[str, Any]:
    return {"id": "oc", "agent": "opencode", "server_url": url, "server_password": password}


def _no_attachments() -> PreparedAgentAttachments:
    return PreparedAgentAttachments(images=[], files=[], prompt_suffix="")


async def _run(profile, *, resume_state=None, model="spark/qwen3", workspace="/tmp/repo"):
    events = []
    async for event in run_opencode_agent(
        profile=profile,
        model=model,
        workspace=workspace,
        messages=[{"role": "user", "content": "fix the bug"}],
        system_prompt="",
        chat_params={},
        resume_state=resume_state,
        attachments=_no_attachments(),
    ):
        events.append(event)
    return events


def _text(events) -> str:
    return "".join(e.text for e in events if isinstance(e, AgentTextDelta))


STREAM_SCRIPT = [
    _event("session.execution.started", sessionID="$SESSION"),
    _event("session.status", sessionID="$SESSION", status={"type": "busy"}),
    _event(
        "session.reasoning.delta",
        sessionID="$SESSION",
        assistantMessageID="m1",
        ordinal=0,
        delta="Let me ",
    ),
    _event(
        "session.reasoning.ended",
        sessionID="$SESSION",
        assistantMessageID="m1",
        ordinal=0,
        text="Let me look.",
    ),
    _event(
        "session.tool.input.started",
        sessionID="$SESSION",
        assistantMessageID="m1",
        id="call_1",
        name="read",
    ),
    _event(
        "session.tool.called",
        sessionID="$SESSION",
        assistantMessageID="m1",
        id="call_1",
        input={"path": "a.py"},
        executed=True,
    ),
    _event(
        "session.tool.success",
        sessionID="$SESSION",
        assistantMessageID="m1",
        id="call_1",
        content=[{"type": "text", "text": "print(1)"}],
        executed=True,
    ),
    _event(
        "session.text.delta",
        sessionID="$SESSION",
        assistantMessageID="m1",
        ordinal=1,
        delta="Fixed ",
    ),
    _event(
        "session.text.delta", sessionID="$SESSION", assistantMessageID="m1", ordinal=1, delta="it"
    ),
    _event(
        "session.text.ended",
        sessionID="$SESSION",
        assistantMessageID="m1",
        ordinal=1,
        text="Fixed it.",
    ),
    _event(
        "session.text.delta",
        sessionID="ses_other",
        assistantMessageID="x",
        ordinal=0,
        delta="NOT MINE",
    ),
    _event("session.execution.succeeded", sessionID="$SESSION"),
]


class OpenCodeV2Tests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mock = MockOpenCode()
        cls.mock.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mock.stop()

    def setUp(self) -> None:
        self.mock.reset()
        self.mock.script = list(STREAM_SCRIPT)

    def _bodies(self, path_suffix: str) -> list[Any]:
        return [body for _, path, body in self.mock.requests if path.endswith(path_suffix)]

    # Discovery and auth

    async def test_detects_v2_and_lists_enabled_models(self):
        models, error = await _probe_opencode_models("opencode", _profile(self.mock.url))
        self.assertIsNone(error)
        self.assertEqual(models, ["spark/qwen3"])

    async def test_missing_password_reports_how_to_fix(self):
        models, error = await _probe_opencode_models("opencode", _profile(self.mock.url, None))
        self.assertIsNone(models)
        self.assertIn("requires a server password", error)

    async def test_wrong_password_reports_rejection(self):
        models, error = await _probe_opencode_models("opencode", _profile(self.mock.url, "bad"))
        self.assertIsNone(models)
        self.assertIn("rejected the server password", error)

    async def test_unreachable_server_reports_connection(self):
        models, error = await _probe_opencode_models(
            "opencode", _profile(f"http://127.0.0.1:{_free_port()}")
        )
        self.assertIsNone(models)
        self.assertIn("Could not connect", error)

    async def test_html_from_api_route_is_reported(self):
        import httpx

        response = httpx.Response(200, headers={"content-type": "text/html"}, text=WEB_UI)
        with self.assertRaisesRegex(opencode_v2.OpenCodeError, "web page instead of API data"):
            opencode_v2.check_response(response, has_password=True)

    # Sessions and streaming

    async def test_streams_text_reasoning_and_tools_without_duplicates(self):
        events = await _run(_profile(self.mock.url))
        self.assertEqual(_text(events), "Fixed it.")
        reasoning = "".join(e.text for e in events if isinstance(e, AgentReasoningDelta))
        self.assertEqual(reasoning, "Let me look.")
        tools = [e for e in events if isinstance(e, AgentToolUpdate)]
        self.assertEqual(tools[-1].status, "completed")
        self.assertEqual(tools[-1].output, "print(1)")
        self.assertEqual(tools[-1].arguments["title"], "read")
        self.assertEqual(tools[-1].arguments["input"], {"path": "a.py"})
        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.resume_state["session_id"], self.mock.session_id)

    async def test_session_opens_workspace_with_model(self):
        await _run(_profile(self.mock.url), workspace="/tmp/repo")
        [create] = self._bodies("/api/session")
        self.assertEqual(create["location"], {"directory": "/tmp/repo"})
        self.assertEqual(create["model"], {"providerID": "spark", "id": "qwen3"})
        [prompt] = self._bodies("/prompt")
        self.assertEqual(set(prompt), {"text"})  # strict 2.x body: no 1.x fields
        self.assertIn("fix the bug", prompt["text"])

    async def test_resume_reuses_session_and_switches_model(self):
        self.mock.sessions.add(self.mock.session_id)
        resume = {"session_id": self.mock.session_id, "model": "spark/old"}
        events = await _run(_profile(self.mock.url), resume_state=resume)
        self.assertEqual(self._bodies("/api/session"), [])  # no new session
        self.assertEqual(
            self._bodies("/model"), [{"model": {"providerID": "spark", "id": "qwen3"}}]
        )
        self.assertEqual(_text(events), "Fixed it.")

    async def test_resume_of_missing_session_starts_a_new_one(self):
        events = await _run(_profile(self.mock.url), resume_state={"session_id": "ses_gone"})
        self.assertEqual(len(self._bodies("/api/session")), 1)
        self.assertIsInstance(events[-1], AgentDone)

    async def test_execution_failure_surfaces_error(self):
        self.mock.script = [
            _event("session.execution.started", sessionID="$SESSION"),
            _event(
                "session.execution.failed",
                sessionID="$SESSION",
                error={"type": "provider", "message": "model endpoint unreachable"},
            ),
        ]
        events = await _run(_profile(self.mock.url))
        self.assertIsInstance(events[-1], AgentError)
        self.assertIn("model endpoint unreachable", events[-1].message)

    async def test_permission_request_is_shown_not_auto_approved(self):
        self.mock.script = [
            _event("session.execution.started", sessionID="$SESSION"),
            _event(
                "permission.asked",
                id="per_1",
                sessionID="$SESSION",
                action="bash",
                resources=["rm -rf build"],
            ),
            _event("session.execution.succeeded", sessionID="$SESSION"),
        ]
        events = await _run(_profile(self.mock.url))
        self.assertIn("waiting for permission to run `bash` on rm -rf build", _text(events))
        self.assertFalse([p for _, p, _ in self.mock.requests if "/permission" in p])

    async def test_cancel_interrupts_the_session(self):
        self.mock.script = [_event("session.execution.started", sessionID="$SESSION")]
        self.mock.hold_open = True
        task = asyncio.create_task(_run(_profile(self.mock.url)))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if self.mock.active:
                break
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn(
            ("POST", f"/api/session/{self.mock.session_id}/interrupt", None),
            self.mock.requests,
        )

    async def test_finishes_by_polling_when_terminal_event_is_missed(self):
        self.mock.script = [
            _event("session.execution.started", sessionID="$SESSION"),
            _event(
                "session.text.ended",
                sessionID="$SESSION",
                assistantMessageID="m",
                ordinal=0,
                text="done",
            ),
        ]
        events = await asyncio.wait_for(_run(_profile(self.mock.url)), timeout=20)
        self.assertEqual(_text(events), "done")
        self.assertIsInstance(events[-1], AgentDone)


class EventTranslatorTests(unittest.TestCase):
    def test_idle_before_start_does_not_finish(self):
        t = opencode_v2.EventTranslator(SESSION)
        t.translate(_event("session.status", sessionID=SESSION, status={"type": "idle"}))
        self.assertFalse(t.finished)
        t.translate(_event("session.status", sessionID=SESSION, status={"type": "busy"}))
        t.translate(_event("session.status", sessionID=SESSION, status={"type": "idle"}))
        self.assertTrue(t.finished)

    def test_ended_without_deltas_emits_full_text_once(self):
        t = opencode_v2.EventTranslator(SESSION)
        out = t.translate(
            _event(
                "session.text.ended",
                sessionID=SESSION,
                assistantMessageID="m",
                ordinal=0,
                text="hi",
            )
        )
        self.assertEqual([e.text for e in out], ["hi"])

    def test_tool_failure_reports_error(self):
        t = opencode_v2.EventTranslator(SESSION)
        [update] = t.translate(
            _event(
                "session.tool.failed",
                sessionID=SESSION,
                assistantMessageID="m",
                id="c",
                error={"type": "x", "message": "boom"},
                executed=True,
            )
        )
        self.assertEqual((update.status, update.output), ("failed", "boom"))


class ApiVersionTests(unittest.IsolatedAsyncioTestCase):
    async def _detect(self, handler):
        import httpx

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="http://oc", transport=transport) as client:
            return await opencode_v2.detect_api_version(client, {})

    async def test_v1_server_serving_web_ui_is_v1(self):
        import httpx

        version = await self._detect(
            lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text=WEB_UI)
        )
        self.assertEqual(version, opencode_v2.API_V1)

    async def test_v1_server_404_is_v1(self):
        import httpx

        self.assertEqual(await self._detect(lambda r: httpx.Response(404)), opencode_v2.API_V1)

    async def test_v2_json_is_v2(self):
        import httpx

        version = await self._detect(lambda r: httpx.Response(200, json={"data": {}}))
        self.assertEqual(version, opencode_v2.API_V2)


class PasswordAndManagedServerTests(unittest.IsolatedAsyncioTestCase):
    def test_reads_local_service_password(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home, ".config", "opencode")
            config.mkdir(parents=True)
            (config / "service.json").write_text(json.dumps({"password": "from-service"}))
            profile = {"server_url": "http://127.0.0.1:4097", "home": home}
            self.assertEqual(
                resolve_opencode_password(profile, profile["server_url"]), "from-service"
            )
            remote = "http://example.test:4097"
            self.assertEqual(resolve_opencode_password({"home": home}, remote), "")
            explicit = {**profile, "server_password": "explicit"}
            self.assertEqual(resolve_opencode_password(explicit, profile["server_url"]), "explicit")

    async def test_managed_server_startup_failure_is_reported_with_password_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "opencode")
            script.write_text(
                "#!/bin/sh\n"
                'if [ -n "$OPENCODE_SERVER_PASSWORD" ]; then echo "password-provided" >&2; fi\n'
                'echo "bad flag $*" >&2\n'
                "exit 3\n"
            )
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            profile = {"id": "oc", "agent": "opencode", "command": str(script), "server_url": ""}
            events = await _run(profile, workspace=tmp)
        self.assertIsInstance(events[-1], AgentError)
        message = events[-1].message
        self.assertIn("exited during startup (code 3)", message)
        self.assertIn("password-provided", message)
        self.assertIn("--port=", message)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONASYNCIODEBUG", "0")
    unittest.main()
