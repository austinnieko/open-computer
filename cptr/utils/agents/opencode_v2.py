"""OpenCode 2.x server protocol.

OpenCode 2.0 moved its HTTP API under ``/api``, made request bodies strict,
replaced the part-based message events with ``session.*`` events, and always
requires HTTP Basic auth (username ``opencode``). Unknown paths fall through to
the web UI and answer ``200 text/html``, so any 1.x-style request silently gets
HTML back instead of an error.

This module holds the 2.x contract (verified against the v2.2.5 OpenAPI spec and
``packages/schema`` sources) so the adapter and detection can share it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from cptr.utils.agents.events import (
    AgentEvent,
    AgentReasoningDelta,
    AgentTextDelta,
    AgentToolUpdate,
)

API_V1 = "v1"
API_V2 = "v2"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class OpenCodeError(RuntimeError):
    """An OpenCode failure with a message meant for the user."""


class OpenCodeAuthError(OpenCodeError):
    pass


def _is_json(response: httpx.Response) -> bool:
    return "json" in response.headers.get("content-type", "").lower()


def _error_message(response: httpx.Response) -> str:
    if _is_json(response):
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            return payload["message"]
    return f"HTTP {response.status_code}"


def auth_error(has_password: bool) -> OpenCodeAuthError:
    if has_password:
        return OpenCodeAuthError(
            "OpenCode rejected the server password (HTTP 401). Update the OpenCode "
            "Server Password in this agent profile."
        )
    return OpenCodeAuthError(
        "OpenCode requires a server password (HTTP 401). Set the OpenCode Server Password "
        "in this agent profile. For `opencode serve --service` it is the `password` value "
        "in ~/.config/opencode/service.json; otherwise it is OPENCODE_SERVER_PASSWORD."
    )


def check_response(response: httpx.Response, *, has_password: bool) -> None:
    """Raise a user-facing error for a failed 2.x API response."""
    if response.status_code == 401:
        raise auth_error(has_password)
    if response.status_code >= 400:
        raise OpenCodeError(f"OpenCode API error: {_error_message(response)}")
    if response.content and not _is_json(response):
        raise OpenCodeError(
            "OpenCode returned a web page instead of API data. Check that the OpenCode "
            "Server URL points at `opencode serve` (without a path)."
        )


async def detect_api_version(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    """Return ``"v2"`` when the server speaks the 2.x ``/api`` protocol, else ``"v1"``.

    2.x answers ``/api/session/active`` with JSON (200, or 401 without valid
    credentials). 1.x has no such route and serves its web UI or a 404.
    """
    response = await client.get("/api/session/active", headers=headers)
    if _is_json(response) and response.status_code in (200, 401):
        if response.status_code == 401:
            raise auth_error(bool(headers.get("Authorization")))
        return API_V2
    return API_V1


def service_config_password(home: str | None = None) -> str | None:
    """Password of a local ``opencode serve --service`` instance, if readable."""
    base = Path(os.path.expanduser(home)) if home else Path.home()
    config_home = os.environ.get("XDG_CONFIG_HOME") if not home else None
    config_dir = Path(config_home) if config_home else base / ".config"
    path = config_dir / "opencode" / "service.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    password = data.get("password") if isinstance(data, dict) else None
    return password if isinstance(password, str) and password.strip() else None


def is_loopback(server_url: str) -> bool:
    return urlsplit(server_url).hostname in _LOOPBACK_HOSTS


# ── API calls ──────────────────────────────────────────────────────────────


async def _call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    headers: dict[str, str],
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.request(method, path, headers=headers, json=json_body, params=params)
    check_response(response, has_password=bool(headers.get("Authorization")))
    if not response.content:
        return {}
    data = response.json()
    return data if isinstance(data, dict) else {}


def model_ref(model: str) -> dict[str, str]:
    provider_id, _, model_id = model.partition("/")
    if not provider_id or not model_id:
        raise OpenCodeError("OpenCode models must use provider/model format")
    return {"providerID": provider_id, "id": model_id}


async def list_models(client: httpx.AsyncClient, headers: dict[str, str]) -> list[str]:
    payload = await _call(client, "GET", "/api/model", headers)
    models: list[str] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        provider_id, model_id = item.get("providerID"), item.get("id")
        if isinstance(provider_id, str) and isinstance(model_id, str) and provider_id and model_id:
            models.append(f"{provider_id}/{model_id}")
    return models


async def create_session(
    client: httpx.AsyncClient, headers: dict[str, str], *, model: str, workspace: str
) -> str:
    body: dict[str, Any] = {"model": model_ref(model)}
    if workspace:
        body["location"] = {"directory": os.path.abspath(workspace)}
    payload = await _call(client, "POST", "/api/session", headers, json_body=body)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    session_id = data.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise OpenCodeError("OpenCode did not return a session id")
    return session_id


async def session_exists(
    client: httpx.AsyncClient, headers: dict[str, str], session_id: str
) -> bool:
    response = await client.get(f"/api/session/{session_id}", headers=headers)
    if response.status_code == 404:
        return False
    check_response(response, has_password=bool(headers.get("Authorization")))
    return True


async def switch_model(
    client: httpx.AsyncClient, headers: dict[str, str], session_id: str, model: str
) -> None:
    await _call(
        client,
        "POST",
        f"/api/session/{session_id}/model",
        headers,
        json_body={"model": model_ref(model)},
    )


async def prompt(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    session_id: str,
    text: str,
    files: list[dict[str, str]],
) -> None:
    body: dict[str, Any] = {"text": text}
    if files:
        body["files"] = files
    await _call(client, "POST", f"/api/session/{session_id}/prompt", headers, json_body=body)


async def interrupt(client: httpx.AsyncClient, headers: dict[str, str], session_id: str) -> None:
    await _call(client, "POST", f"/api/session/{session_id}/interrupt", headers)


async def is_active(client: httpx.AsyncClient, headers: dict[str, str], session_id: str) -> bool:
    payload = await _call(client, "GET", "/api/session/active", headers)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return session_id in data


# ── Event stream ──────────────────────────────────────────────────────────


class EventTranslator:
    """Turn 2.x ``/api/event`` envelopes for one session into agent events.

    Text and reasoning stream as ``*.delta`` fragments followed by a full
    ``*.ended`` value; only the part of ``*.ended`` not already streamed is
    emitted, so replies never appear twice.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._streamed: dict[tuple[str, str, int], str] = {}
        self._tool_names: dict[str, str] = {}
        self._tool_inputs: dict[str, dict[str, Any]] = {}
        self._permission_requests: set[tuple[str, tuple[str, ...]]] = set()
        self.started = False
        self.finished = False
        self.error: str | None = None
        # Summed from session.step.ended / step.failed (tokens and USD cost).
        self.usage: dict[str, Any] = {}

    def matches(self, envelope: dict[str, Any]) -> bool:
        data = envelope.get("data")
        return isinstance(data, dict) and data.get("sessionID") == self.session_id

    def _stream(self, kind: str, data: dict[str, Any], text: str, *, full: bool) -> str:
        key = (kind, str(data.get("assistantMessageID") or ""), int(data.get("ordinal") or 0))
        previous = self._streamed.get(key, "")
        if not full:
            self._streamed[key] = previous + text
            return text
        self._streamed[key] = text
        return text[len(previous) :] if text.startswith(previous) else ""

    def _tool(
        self, data: dict[str, Any], status: str, output: str | None = None
    ) -> AgentToolUpdate:
        call_id = str(data.get("id") or "")
        name = self._tool_names.get(call_id, "Agent tool")
        arguments: dict[str, Any] = {"title": name}
        if call_id in self._tool_inputs:
            arguments["input"] = self._tool_inputs[call_id]
        if isinstance(data.get("metadata"), dict):
            arguments["metadata"] = data["metadata"]
        return AgentToolUpdate(
            call_id=call_id, name="agent_tool", status=status, arguments=arguments, output=output
        )

    def translate(self, envelope: dict[str, Any]) -> list[AgentEvent]:
        if not self.matches(envelope):
            return []
        event_type = envelope.get("type")
        data = envelope["data"]
        if event_type != "session.status" or (
            isinstance(data.get("status"), dict) and data["status"].get("type") == "busy"
        ):
            self.started = True
        if event_type == "session.text.delta" and isinstance(data.get("delta"), str):
            text = self._stream("text", data, data["delta"], full=False)
            return [AgentTextDelta(text)] if text else []
        if event_type == "session.text.ended" and isinstance(data.get("text"), str):
            text = self._stream("text", data, data["text"], full=True)
            return [AgentTextDelta(text)] if text else []
        if event_type == "session.reasoning.delta" and isinstance(data.get("delta"), str):
            text = self._stream("reasoning", data, data["delta"], full=False)
            return [AgentReasoningDelta(text)] if text else []
        if event_type == "session.reasoning.ended" and isinstance(data.get("text"), str):
            text = self._stream("reasoning", data, data["text"], full=True)
            return [AgentReasoningDelta(text)] if text else []
        if event_type == "session.tool.input.started" and data.get("id"):
            self._tool_names[str(data["id"])] = str(data.get("name") or "Agent tool")
            return [self._tool(data, "in_progress")]
        if event_type == "session.tool.called" and data.get("id"):
            if isinstance(data.get("input"), dict):
                self._tool_inputs[str(data["id"])] = data["input"]
            return [self._tool(data, "in_progress")]
        if event_type == "session.tool.progress" and data.get("id"):
            return [self._tool(data, "in_progress")]
        if event_type == "session.tool.success" and data.get("id"):
            return [self._tool(data, "completed", _content_text(data.get("content")))]
        if event_type == "session.tool.failed" and data.get("id"):
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            output = _content_text(data.get("content")) or str(
                error.get("message") or "Tool failed"
            )
            return [self._tool(data, "failed", output)]
        if event_type == "permission.asked":
            return self._permission_notice(data)
        if event_type in ("session.step.ended", "session.step.failed"):
            self._add_usage(data)
            return []
        if event_type == "session.execution.failed":
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            self.error = str(error.get("message") or "OpenCode run failed")
            self.finished = True
        elif event_type in ("session.execution.succeeded", "session.execution.interrupted"):
            self.finished = True
        elif event_type == "session.status":
            status = data.get("status") if isinstance(data.get("status"), dict) else {}
            # Idle only ends the run once it has started; an idle status can
            # arrive between sending the prompt and the run beginning.
            if status.get("type") == "idle" and self.started:
                self.finished = True
        elif event_type == "session.idle" and self.started:
            self.finished = True
        return []

    def _add_usage(self, data: dict[str, Any]) -> None:
        """Record a step's tokens and cost.

        The standard fields hold the latest step (its input is the context
        size, which compaction and the context meter rely on); ``turn`` holds
        the sums over every step in this prompt.
        """
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}

        def num(value: Any) -> float:
            return value if isinstance(value, (int, float)) and value > 0 else 0

        step = {
            "input_tokens": int(num(tokens.get("input"))),
            "output_tokens": int(num(tokens.get("output"))),
            "reasoning_tokens": int(num(tokens.get("reasoning"))),
            "cache_read_input_tokens": int(num(cache.get("read"))),
            "cache_creation_input_tokens": int(num(cache.get("write"))),
        }
        turn = dict(self.usage.get("turn") or {})
        for key, value in step.items():
            turn[key] = turn.get(key, 0) + value
        turn["total_tokens"] = turn["input_tokens"] + turn["output_tokens"]
        turn["cost_usd"] = round(turn.get("cost_usd", 0) + num(data.get("cost")), 6)
        turn["steps"] = turn.get("steps", 0) + 1
        self.usage = {
            **step,
            "total_tokens": step["input_tokens"] + step["output_tokens"],
            "turn": turn,
        }

    def _permission_notice(self, data: dict[str, Any]) -> list[AgentEvent]:
        """One short line per new permission request; the how-to only once."""
        action = str(data.get("action") or "a tool")
        resources = tuple(str(r) for r in data.get("resources") or [])
        key = (action, resources)
        if key in self._permission_requests:
            return []
        first = not self._permission_requests
        self._permission_requests.add(key)
        shown = ", ".join(f"`{r}`" for r in resources[:3])
        if len(resources) > 3:
            shown += f" and {len(resources) - 3} more"
        line = f"Waiting for permission: `{action}`" + (f" on {shown}" if shown else "")
        if first:
            line += (
                ". Approve it in the OpenCode app or TUI, or allow it in your OpenCode "
                "agent permissions"
            )
        return [AgentTextDelta(f"\n\n⏸ {line}.\n\n" if first else f"\n⏸ {line}.\n")]


def _content_text(content: Any) -> str | None:
    if not isinstance(content, list):
        return None
    texts = [
        str(part.get("text"))
        for part in content
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
    ]
    return "\n".join(texts) or None


def parse_sse_line(line: str) -> dict[str, Any] | None:
    if not line.startswith("data:"):
        return None
    try:
        payload = json.loads(line[5:].strip())
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None
