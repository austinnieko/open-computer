"""OpenCode server adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import socket
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import httpx

from cptr.utils.agents import opencode_v2
from cptr.utils.agents.attachments import PreparedAgentAttachments
from cptr.utils.agents.events import (
    AgentDone,
    AgentError,
    AgentEvent,
    AgentTextDelta,
    AgentToolUpdate,
)
from cptr.utils.agents.opencode_v2 import OpenCodeError
from cptr.utils.agents.prompts import turn_prompt_text
from cptr.utils.identity import env_for, preexec_for


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_OPENCODE_ACTIVITY = object()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def opencode_server_url_candidates(server_url: str) -> list[str]:
    server_url = server_url.strip()
    if not server_url:
        return []
    parsed = urlsplit(server_url)
    in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    if not in_container or parsed.hostname not in _LOOPBACK_HOSTS:
        return [server_url]
    docker_url = urlunsplit(
        (
            parsed.scheme,
            f"host.docker.internal{':' + str(parsed.port) if parsed.port else ''}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )
    return [server_url] if docker_url == server_url else [server_url, docker_url]


async def _server_url_from_stdout(
    proc: asyncio.subprocess.Process, port: int, stderr_tail: deque[str] | None = None
) -> str:
    assert proc.stdout is not None
    fallback = f"http://127.0.0.1:{port}"
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=1)
        except asyncio.TimeoutError:
            continue
        if not line:
            break
        match = re.search(r"(https?://[^\s]+)", line.decode(errors="replace"))
        if match:
            return match.group(1)
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=0.2)
    if proc.returncode is not None:
        detail = " ".join(stderr_tail or []).strip()
        raise OpenCodeError(
            f"OpenCode server exited during startup (code {proc.returncode})"
            + (f": {detail[-500:]}" if detail else ".")
        )
    return fallback


@asynccontextmanager
async def _opencode_server(profile: dict[str, Any], workspace: str, identity=None):
    server_url = str(profile.get("server_url") or "").strip()
    if server_url:
        yield server_url, None, None
        return

    env = (
        env_for(identity, workspace or os.getcwd())
        if identity and identity.is_pam
        else os.environ.copy()
    )
    if profile.get("home"):
        env["HOME"] = os.path.expanduser(str(profile["home"]))
    port = _free_port()
    # OpenCode 2.x always requires a password and invents a random one when none
    # is given, so the server we start gets one we know. Harmless for 1.x.
    password = str(profile.get("server_password") or "").strip() or secrets.token_urlsafe(32)
    proc = await asyncio.create_subprocess_exec(
        str(profile["command"]),
        "serve",
        "--hostname=127.0.0.1",
        f"--port={port}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace or os.getcwd(),
        env={**env, "OPENCODE_CONFIG_CONTENT": "{}", "OPENCODE_SERVER_PASSWORD": password},
        preexec_fn=preexec_for(identity) if identity and identity.is_pam else None,
    )
    stderr_tail: deque[str] = deque(maxlen=20)
    stderr_task = asyncio.create_task(_drain_stderr(proc, stderr_tail))
    try:
        yield await _server_url_from_stdout(proc, port, stderr_tail), proc, password
    finally:
        stderr_task.cancel()
        with suppress(asyncio.CancelledError):
            await stderr_task
        if proc.returncode is None:
            proc.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


async def _drain_stderr(proc: asyncio.subprocess.Process, tail: deque[str] | None = None) -> None:
    assert proc.stderr is not None
    while True:
        try:
            line = await proc.stderr.readline()
        except ValueError:
            continue
        if not line:
            break
        if tail is not None:
            tail.append(line.decode(errors="replace").strip())


def resolve_opencode_password(
    profile: dict[str, Any], server_url: str, managed_password: str | None = None
) -> str:
    """Profile password, else our managed server's, else a local service's own."""
    password = str(profile.get("server_password") or "").strip()
    if password or managed_password:
        return password or str(managed_password)
    if server_url and opencode_v2.is_loopback(server_url):
        return opencode_v2.service_config_password(profile.get("home")) or ""
    return ""


def _headers(profile: dict[str, Any], password: str | None = None) -> dict[str, str]:
    if password is None:
        password = str(profile.get("server_password") or "")
    password = password.strip()
    if not password:
        return {}
    token = base64.b64encode(f"opencode:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def _request(
    client: httpx.AsyncClient,
    method: str,
    paths: list[str],
    *,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for path in paths:
        try:
            response = await client.request(method, f"/{path}", headers=headers, json=json_body)
            response.raise_for_status()
            if not response.content:
                return {}
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001 - try alternate generated route names.
            last_error = exc
    if last_error:
        raise last_error
    return {}


def _session_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _parse_model(model: str) -> dict[str, str]:
    provider_id, _, model_id = model.partition("/")
    if not provider_id or not model_id:
        raise RuntimeError("OpenCode models must use provider/model format")
    return {"providerID": provider_id, "modelID": model_id}


def _opencode_parts(prompt: str, attachments: PreparedAgentAttachments) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if prompt.strip():
        parts.append({"type": "text", "text": prompt})
    for item in [*attachments.images, *attachments.files]:
        parts.append(
            {
                "type": "file",
                "mime": item.mime_type,
                "filename": item.name,
                "url": Path(item.path).resolve().as_uri(),
            }
        )
    return parts


def _resume_session_id(resume_state: dict[str, Any] | None) -> str | None:
    if not resume_state:
        return None
    value = resume_state.get("session_id")
    return value if isinstance(value, str) and value.strip() else None


async def _create_opencode_session(
    client: httpx.AsyncClient,
    headers: dict[str, str],
) -> str:
    session_payload = await _request(
        client,
        "POST",
        ["session.create", "session/create", "session"],
        headers=headers,
        json_body={"title": "cptr", "permission": []},
    )
    session = _session_data(session_payload)
    session_id = session.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("OpenCode did not return a session id")
    return session_id


async def _start_opencode_prompt(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    session_id: str,
    parsed_model: dict[str, str],
    parts: list[dict[str, Any]],
) -> None:
    await _request(
        client,
        "POST",
        [
            f"session/{session_id}/prompt_async",
            f"session/{session_id}/message",
            "session.promptAsync",
            "session/promptAsync",
            "session/prompt",
        ],
        headers=headers,
        json_body={
            "sessionID": session_id,
            "model": parsed_model,
            "parts": parts,
        },
    )


def _role_update_from_event(event: dict[str, Any]) -> tuple[str, str] | None:
    if event.get("type") != "message.updated":
        return None
    props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
    info = props.get("info") if isinstance(props.get("info"), dict) else {}
    message_id = info.get("id")
    role = info.get("role")
    if isinstance(message_id, str) and isinstance(role, str):
        return message_id, role
    return None


def _text_from_event(
    event: dict[str, Any],
    emitted: dict[str, str],
    message_roles: dict[str, str],
) -> str | None:
    event_type = event.get("type")
    props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
    if event_type == "message.part.delta":
        message_id = props.get("messageID")
        if isinstance(message_id, str) and message_roles.get(message_id) == "user":
            return None
        delta = props.get("delta")
        part_id = props.get("partID")
        if isinstance(part_id, str) and isinstance(delta, str):
            emitted[part_id] = f"{emitted.get(part_id, '')}{delta}"
        return delta if isinstance(delta, str) and delta else None
    if event_type == "message.part.updated":
        part = props.get("part") if isinstance(props.get("part"), dict) else {}
        if part.get("type") not in ("text", "reasoning"):
            return None
        message_id = part.get("messageID")
        if isinstance(message_id, str) and message_roles.get(message_id) == "user":
            return None
        part_id = part.get("id")
        text = part.get("text")
        if not isinstance(part_id, str) or not isinstance(text, str):
            return None
        previous = emitted.get(part_id, "")
        if text.startswith(previous):
            delta = text[len(previous) :]
        else:
            delta = text
        emitted[part_id] = text
        return delta or None
    return None


def _tool_from_event(event: dict[str, Any]) -> AgentToolUpdate | None:
    if event.get("type") != "message.part.updated":
        return None
    props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
    part = props.get("part") if isinstance(props.get("part"), dict) else {}
    if part.get("type") != "tool":
        return None
    call_id = part.get("callID") or part.get("id")
    if not isinstance(call_id, str) or not call_id.strip():
        return None
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    tool = str(part.get("tool") or state.get("title") or "Agent tool").strip()
    status = _opencode_tool_status(state.get("status"))
    output = _opencode_tool_output(state)
    return AgentToolUpdate(
        call_id=call_id.strip(),
        name="agent_tool",
        status=status,
        arguments={"title": tool, **({"state": state} if state else {})},
        output=output,
    )


def _opencode_tool_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "completed":
        return "completed"
    if normalized == "error":
        return "failed"
    if normalized == "pending":
        return "pending"
    return "in_progress"


def _opencode_tool_output(state: dict[str, Any]) -> str | None:
    for key in ("output", "result", "message", "error"):
        value = state.get(key)
        if value is None:
            continue
        return value if isinstance(value, str) else json.dumps(value, indent=2)
    return None


async def run_opencode_agent(
    *,
    profile: dict[str, Any],
    model: str,
    workspace: str,
    messages: list[dict[str, Any]],
    system_prompt: str,
    chat_params: dict[str, Any],
    resume_state: dict[str, Any] | None,
    attachments: PreparedAgentAttachments,
    identity=None,
) -> AsyncIterator[AgentEvent]:
    del chat_params
    try:
        async with _opencode_server(profile, workspace, identity) as (
            server_url,
            _proc,
            managed_password,
        ):
            headers = _headers(
                profile, resolve_opencode_password(profile, server_url, managed_password)
            )
            urls = opencode_server_url_candidates(server_url)
            last_connect_error: Exception | None = None
            for index, candidate_url in enumerate(urls):
                try:
                    async with httpx.AsyncClient(
                        base_url=candidate_url,
                        timeout=httpx.Timeout(None, connect=5),
                    ) as client:
                        api = await opencode_v2.detect_api_version(client, headers)
                        if api == opencode_v2.API_V2:
                            async for event in _run_v2(
                                client,
                                headers,
                                profile=profile,
                                model=model,
                                workspace=workspace,
                                messages=messages,
                                system_prompt=system_prompt,
                                resume_state=resume_state,
                                attachments=attachments,
                            ):
                                yield event
                            return
                        session_id = _resume_session_id(resume_state)
                        resumed = bool(session_id)
                        if session_id is None:
                            session_id = await _create_opencode_session(client, headers)

                        parsed_model = _parse_model(model)
                        while True:
                            emitted: dict[str, str] = {}
                            event_queue: asyncio.Queue[AgentEvent | object | None] = asyncio.Queue()
                            event_task = asyncio.create_task(
                                _collect_opencode_events(
                                    client, headers, session_id, emitted, event_queue
                                )
                            )

                            prompt = turn_prompt_text(messages, system_prompt, resumed=resumed)
                            parts = _opencode_parts(prompt, attachments)
                            try:
                                try:
                                    await _start_opencode_prompt(
                                        client, headers, session_id, parsed_model, parts
                                    )
                                except Exception:
                                    if not resumed:
                                        raise
                                    event_task.cancel()
                                    with suppress(asyncio.CancelledError):
                                        await event_task
                                    session_id = await _create_opencode_session(client, headers)
                                    resumed = False
                                    continue

                                last_activity = asyncio.get_running_loop().time()
                                while True:
                                    try:
                                        item = await asyncio.wait_for(event_queue.get(), timeout=1)
                                    except asyncio.TimeoutError:
                                        with suppress(Exception):
                                            payload = await asyncio.wait_for(
                                                _request(
                                                    client,
                                                    "GET",
                                                    ["session/status", "session.status"],
                                                    headers=headers,
                                                ),
                                                timeout=5,
                                            )
                                            status = _session_data(payload).get(session_id)
                                            if (
                                                not isinstance(status, dict)
                                                or status.get("type") == "idle"
                                            ):
                                                break
                                        if asyncio.get_running_loop().time() - last_activity >= 600:
                                            raise RuntimeError(
                                                "OpenCode stopped sending events for 10 minutes."
                                            )
                                        continue
                                    if item is _OPENCODE_ACTIVITY:
                                        last_activity = asyncio.get_running_loop().time()
                                        continue
                                    if item is None:
                                        break
                                    last_activity = asyncio.get_running_loop().time()
                                    yield item
                            except asyncio.CancelledError:
                                with suppress(Exception):
                                    await _request(
                                        client,
                                        "POST",
                                        [
                                            f"session/{session_id}/abort",
                                            "session.abort",
                                            "session/abort",
                                        ],
                                        headers=headers,
                                        json_body={"sessionID": session_id},
                                    )
                                raise
                            finally:
                                event_task.cancel()
                                with suppress(asyncio.CancelledError):
                                    await event_task
                            break

                        yield AgentDone(
                            resume_state={
                                "profile_id": profile["id"],
                                "session_id": session_id,
                                "workspace": workspace,
                                "model": model,
                            }
                        )
                    return
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    last_connect_error = exc
                    if index < len(urls) - 1:
                        continue
                    if len(urls) > 1:
                        port = urlsplit(server_url).port or 4096
                        raise RuntimeError(
                            "Unable to connect to OpenCode from Docker. If OpenCode is running "
                            "on the host, start it with "
                            f"`opencode serve --hostname 0.0.0.0 --port {port}` and set the "
                            f"OpenCode Server URL to `http://host.docker.internal:{port}`. "
                            "On Linux Docker, add "
                            "`--add-host=host.docker.internal:host-gateway` if that hostname "
                            "is unavailable."
                        ) from exc
                    raise
            if last_connect_error:
                raise last_connect_error
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced in chat.
        yield AgentError(str(exc))


async def _collect_opencode_events(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    session_id: str,
    emitted: dict[str, str],
    queue: asyncio.Queue[AgentEvent | object | None],
) -> None:
    message_roles: dict[str, str] = {}
    try:
        for path in ("event.subscribe", "event/subscribe", "event"):
            try:
                async with client.stream("GET", f"/{path}", headers=headers) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        with suppress(Exception):
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                continue
                            props = (
                                event.get("properties")
                                if isinstance(event.get("properties"), dict)
                                else {}
                            )
                            if props.get("sessionID") != session_id:
                                continue
                            await queue.put(_OPENCODE_ACTIVITY)
                            role_update = _role_update_from_event(event)
                            if role_update:
                                message_roles[role_update[0]] = role_update[1]
                            text = _text_from_event(event, emitted, message_roles)
                            if text:
                                await queue.put(AgentTextDelta(text))
                            tool = _tool_from_event(event)
                            if tool:
                                await queue.put(tool)
                            status = (
                                props.get("status") if isinstance(props.get("status"), dict) else {}
                            )
                            if (
                                event.get("type") == "session.status"
                                and status.get("type") == "idle"
                            ):
                                await queue.put(None)
                                return
            except Exception:
                continue
    except Exception:
        await queue.put(None)
    await queue.put(None)


# ── OpenCode 2.x ───────────────────────────────────────────────────────────

_V2_CONNECT_WAIT_SECONDS = 5
_V2_START_GRACE_SECONDS = 15
_V2_IDLE_POLL_SECONDS = 1
_V2_MAX_RECONNECTS = 5


async def _run_v2(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    profile: dict[str, Any],
    model: str,
    workspace: str,
    messages: list[dict[str, Any]],
    system_prompt: str,
    resume_state: dict[str, Any] | None,
    attachments: PreparedAgentAttachments,
) -> AsyncIterator[AgentEvent]:
    session_id = _resume_session_id(resume_state)
    if session_id and not await opencode_v2.session_exists(client, headers, session_id):
        session_id = None
    resumed = bool(session_id)
    if session_id is None:
        session_id = await opencode_v2.create_session(
            client, headers, model=model, workspace=workspace
        )
    elif (resume_state or {}).get("model") != model:
        await opencode_v2.switch_model(client, headers, session_id, model)

    translator = opencode_v2.EventTranslator(session_id)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    connected = asyncio.Event()
    loop = asyncio.get_running_loop()

    def start_stream() -> asyncio.Task:
        return asyncio.create_task(
            _collect_v2_events(client, headers, translator, queue, connected)
        )

    stream_task = start_stream()
    try:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(connected.wait(), timeout=_V2_CONNECT_WAIT_SECONDS)
        if stream_task.done() and stream_task.exception():
            raise stream_task.exception()  # e.g. 401 on the event stream

        text = turn_prompt_text(messages, system_prompt, resumed=resumed)
        files = [
            {"uri": Path(item.path).resolve().as_uri(), "name": item.name}
            for item in [*attachments.images, *attachments.files]
        ]
        await opencode_v2.prompt(client, headers, session_id, text, files)
        sent_at = last_activity = loop.time()
        reconnects = 0

        while not translator.finished:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=_V2_IDLE_POLL_SECONDS)
            except asyncio.TimeoutError:
                item = None
            if item is _OPENCODE_ACTIVITY:
                last_activity = loop.time()
                continue
            if isinstance(item, BaseException):
                # The event stream dropped; reconnect unless the run already ended.
                if reconnects >= _V2_MAX_RECONNECTS:
                    raise OpenCodeError(f"Lost the OpenCode event stream: {item}")
                reconnects += 1
                await asyncio.sleep(min(reconnects, 3))
                stream_task = start_stream()
                continue
            if item is not None:
                last_activity = loop.time()
                yield item
                continue
            # Quiet second: ask OpenCode whether the session is still running.
            if loop.time() - last_activity < _V2_IDLE_POLL_SECONDS:
                continue
            try:
                active = await asyncio.wait_for(
                    opencode_v2.is_active(client, headers, session_id), timeout=5
                )
            except opencode_v2.OpenCodeAuthError:
                raise
            except Exception:  # noqa: BLE001 - transient; keep waiting on events.
                continue
            if not active and (
                translator.started or loop.time() - sent_at > _V2_START_GRACE_SECONDS
            ):
                break

        # Drain anything the stream delivered alongside the terminal event.
        while not queue.empty():
            item = queue.get_nowait()
            if item is not _OPENCODE_ACTIVITY and not isinstance(item, BaseException) and item:
                yield item
        if translator.error:
            raise OpenCodeError(translator.error)
    except asyncio.CancelledError:
        with suppress(Exception):
            await asyncio.wait_for(opencode_v2.interrupt(client, headers, session_id), timeout=5)
        raise
    finally:
        stream_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await stream_task

    yield AgentDone(
        usage=dict(translator.usage) or None,
        resume_state={
            "profile_id": profile["id"],
            "session_id": session_id,
            "workspace": workspace,
            "model": model,
        },
    )


async def _collect_v2_events(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    translator: opencode_v2.EventTranslator,
    queue: asyncio.Queue[Any],
    connected: asyncio.Event,
) -> None:
    try:
        async with client.stream(
            "GET",
            "/api/event",
            headers={**headers, "Accept": "text/event-stream"},
            timeout=httpx.Timeout(None, connect=5),
        ) as response:
            if response.status_code == 401:
                raise opencode_v2.auth_error(bool(headers.get("Authorization")))
            response.raise_for_status()
            async for line in response.aiter_lines():
                envelope = opencode_v2.parse_sse_line(line)
                if envelope is None:
                    continue
                if envelope.get("type") == "server.connected":
                    connected.set()
                    continue
                if not translator.matches(envelope):
                    continue
                await queue.put(_OPENCODE_ACTIVITY)
                for event in translator.translate(envelope):
                    await queue.put(event)
                if translator.finished:
                    await queue.put(None)
                    return
        await queue.put(ConnectionError("event stream closed"))
    except asyncio.CancelledError:
        raise
    except opencode_v2.OpenCodeAuthError:
        connected.set()
        raise
    except Exception as exc:  # noqa: BLE001 - reported to the run loop for reconnect.
        await queue.put(exc)
