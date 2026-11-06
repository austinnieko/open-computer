"""OpenCode 1.x native protocol: one turn, with event-stream recovery.

The 1.x server exposes ``/session/...`` routes and one ``/event`` SSE stream
for the whole instance. That stream carries every session's events plus a
``server.heartbeat`` every 10 seconds (verified against v1.19.5,
``server/routes/instance/httpapi/handlers/event.ts``).

What this module guarantees for a turn:

* **A quiet agent is not a dead agent.** A long tool run or a slow model
  sends no session events for minutes. As long as heartbeats arrive and
  ``/session/status`` says the session is busy, the turn keeps waiting and
  reports ``waiting`` with the time of the last real event. There is no
  fixed "N minutes without events" failure.
* **A dead stream is noticed and replaced.** A closed stream, a transport
  error, or 35 seconds without a heartbeat (half-open connection) all
  trigger a resubscribe with exponential backoff, bounded per turn.
* **Nothing is lost or repeated.** After a gap the turn's messages are read
  back from ``/session/{id}/message`` and fed through the same translator,
  which only emits text it has not emitted yet and updates tool calls by
  id. Recovery only ever *reads*: the prompt is sent once, so a reconnect
  can't start the work again or re-run a tool.
* **Failure is explicit.** If the stream can't be restored, or the server
  stops answering for two minutes, the turn fails with a message saying so,
  and the caller keeps the OpenCode session id so the next message
  continues the same session.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

from cptr.utils.agents.events import (
    AgentEvent,
    AgentReasoningDelta,
    AgentStatus,
    AgentTextDelta,
    AgentToolUpdate,
)
from cptr.utils.agents.opencode_v2 import OpenCodeAuthError, OpenCodeError, auth_error

# Tunables (module level so tests can shorten them).
STREAM_STALE_SECONDS = 35.0
"""No line at all (not even a heartbeat) for this long: the stream is dead."""
STATUS_POLL_SECONDS = 3.0
STATUS_REPORT_SECONDS = 15.0
WAITING_AFTER_SECONDS = 20.0
"""Quiet this long while busy: report ``waiting`` (with the last activity time)."""
START_GRACE_SECONDS = 15.0
"""An idle status this soon after prompting may predate the prompt."""
RECONNECT_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
MAX_RECONNECTS = 8
"""Consecutive failed resubscribes before the turn gives up."""
UNREACHABLE_SECONDS = 120.0
SUBSCRIBE_WAIT_SECONDS = 5.0
RECONCILE_MESSAGE_LIMIT = 30
EVENT_PATHS = ("event", "event.subscribe", "event/subscribe")


class OpenCodeHTTPError(OpenCodeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def describe_response(method: str, path: str, response: httpx.Response) -> str:
    """A short, credential-free description of a failed OpenCode response."""
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for value in (payload.get("message"), data.get("message"), payload.get("error"), payload.get("name")):
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
    elif response.content and "html" not in response.headers.get("content-type", ""):
        detail = response.text.strip()
    detail = " ".join(detail.split())[:300]
    return f"OpenCode {method} /{path} failed (HTTP {response.status_code})" + (
        f": {detail}" if detail else ""
    )


def _is_json(response: httpx.Response) -> bool:
    return "json" in response.headers.get("content-type", "").lower()


async def request(
    client: httpx.AsyncClient,
    method: str,
    paths: list[str] | tuple[str, ...],
    *,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """Call the first of ``paths`` the server actually serves.

    Only "this route doesn't exist here" moves on to the next name: a 404/405,
    or the web UI answering instead of the API. Any other failure is real and
    raised as-is, so a rejected request never silently retries under a
    different contract (e.g. the blocking ``/message`` instead of
    ``/prompt_async``).
    """
    missing: str | None = None
    for path in paths:
        response = await client.request(
            method, f"/{path}", headers=headers, json=json_body, params=params
        )
        if response.status_code == 401:
            raise auth_error(bool(headers.get("Authorization")))
        if response.status_code in (404, 405) or (
            response.status_code < 300 and response.content and not _is_json(response)
        ):
            missing = describe_response(method, path, response)
            continue
        if response.status_code >= 400:
            raise OpenCodeHTTPError(describe_response(method, path, response), response.status_code)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise OpenCodeHTTPError(f"OpenCode {method} /{path} returned invalid JSON") from exc
    raise OpenCodeHTTPError(missing or "OpenCode route not found", 404)


async def request_dict(client, method, paths, **kwargs) -> dict[str, Any]:
    data = await request(client, method, paths, **kwargs)
    return data if isinstance(data, dict) else {}


def session_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


async def session_status(
    client: httpx.AsyncClient, headers: dict[str, str], session_id: str
) -> dict[str, Any] | None:
    """The session's status (``busy``/``retry``), or None when idle."""
    payload = await request_dict(client, "GET", ["session/status", "session.status"], headers=headers)
    status = session_data(payload).get(session_id)
    if not isinstance(status, dict) or status.get("type") == "idle":
        return None
    return status


async def session_exists(client: httpx.AsyncClient, headers: dict[str, str], session_id: str) -> bool:
    try:
        await request(client, "GET", [f"session/{session_id}"], headers=headers)
    except OpenCodeHTTPError as exc:
        if exc.status == 404:
            return False
        raise
    return True


async def recent_messages(
    client: httpx.AsyncClient, headers: dict[str, str], session_id: str
) -> list[dict[str, Any]]:
    data = await request(
        client,
        "GET",
        [f"session/{session_id}/message"],
        headers=headers,
        params={"limit": RECONCILE_MESSAGE_LIMIT},
    )
    if isinstance(data, dict):
        data = data.get("data")
    return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


# ── Event translation ─────────────────────────────────────────────────────


def tool_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "completed":
        return "completed"
    if normalized == "error":
        return "failed"
    if normalized == "pending":
        return "pending"
    return "in_progress"


def tool_output(state: dict[str, Any]) -> str | None:
    for key in ("output", "result", "message", "error"):
        value = state.get(key)
        if value is None:
            continue
        return value if isinstance(value, str) else json.dumps(value, indent=2)
    return None


def tool_update(part: dict[str, Any]) -> AgentToolUpdate | None:
    call_id = part.get("callID") or part.get("id")
    if not isinstance(call_id, str) or not call_id.strip():
        return None
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    tool = str(part.get("tool") or state.get("title") or "Agent tool").strip()
    return AgentToolUpdate(
        call_id=call_id.strip(),
        name="agent_tool",
        status=tool_status(state.get("status")),
        arguments={"title": tool, **({"state": state} if state else {})},
        output=tool_output(state),
    )


def _error_text(error: Any) -> str | None:
    if not isinstance(error, dict):
        return str(error) if error else None
    name = str(error.get("name") or "")
    if name == "MessageAbortedError":
        return None  # the user pressed Stop
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    message = data.get("message") or error.get("message")
    return str(message or name or "OpenCode reported an error")


class Translator:
    """Turns 1.x bus events (live or replayed) into agent events, once each."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.emitted: dict[str, str] = {}
        self.roles: dict[str, str] = {}
        self.part_kinds: dict[str, str] = {}
        self.user_message_id: str | None = None
        self.sent_ms: float | None = None
        self.started = False
        self.idle = False
        self.error: str | None = None
        self.retry: dict[str, Any] | None = None
        self._permissions: set[tuple[str, tuple[str, ...]]] = set()

    def _ours(self, props: dict[str, Any]) -> bool:
        return props.get("sessionID") == self.session_id

    def _is_turn_message(self, info: dict[str, Any]) -> bool:
        created = (info.get("time") or {}).get("created") if isinstance(info.get("time"), dict) else None
        if self.sent_ms is None or not isinstance(created, (int, float)):
            return self.sent_ms is not None
        return created >= self.sent_ms - 5_000

    def _note_message(self, info: dict[str, Any]) -> None:
        message_id, role = info.get("id"), info.get("role")
        if not isinstance(message_id, str) or not isinstance(role, str):
            return
        self.roles[message_id] = role
        if role == "user" and self.user_message_id is None and self._is_turn_message(info):
            self.user_message_id = message_id
        if role == "assistant" and self.sent_ms is not None:
            self.started = True

    def _part(self, part: dict[str, Any]) -> list[AgentEvent]:
        kind = part.get("type")
        part_id = part.get("id")
        message_id = part.get("messageID")
        if isinstance(part_id, str) and isinstance(kind, str):
            self.part_kinds[part_id] = kind
        if isinstance(message_id, str) and self.roles.get(message_id) == "user":
            return []
        if kind == "tool":
            update = tool_update(part)
            return [update] if update else []
        if kind not in ("text", "reasoning"):
            return []
        text = part.get("text")
        if not isinstance(part_id, str) or not isinstance(text, str):
            return []
        previous = self.emitted.get(part_id, "")
        delta = text[len(previous) :] if text.startswith(previous) else text
        self.emitted[part_id] = text
        if not delta:
            return []
        return [AgentReasoningDelta(delta) if kind == "reasoning" else AgentTextDelta(delta)]

    def translate(self, event: dict[str, Any]) -> tuple[bool, list[AgentEvent]]:
        """Return (was this event about our session, events to emit)."""
        event_type = event.get("type")
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        if not self._ours(props):
            return False, []
        if event_type == "message.updated":
            info = props.get("info") if isinstance(props.get("info"), dict) else {}
            self._note_message(info)
            return True, []
        if event_type == "message.part.updated":
            part = props.get("part") if isinstance(props.get("part"), dict) else {}
            return True, self._part(part)
        if event_type == "message.part.delta":
            message_id = props.get("messageID")
            if isinstance(message_id, str) and self.roles.get(message_id) == "user":
                return True, []
            delta, part_id = props.get("delta"), props.get("partID")
            if props.get("field", "text") != "text" or not isinstance(delta, str) or not delta:
                return True, []
            if isinstance(part_id, str):
                self.emitted[part_id] = f"{self.emitted.get(part_id, '')}{delta}"
                if self.part_kinds.get(part_id) == "reasoning":
                    return True, [AgentReasoningDelta(delta)]
            return True, [AgentTextDelta(delta)]
        if event_type == "session.status":
            status = props.get("status") if isinstance(props.get("status"), dict) else {}
            kind = status.get("type")
            if kind == "idle":
                self.idle = True
                self.retry = None
            elif kind == "retry":
                self.started = True
                self.retry = status
            elif kind == "busy":
                self.started = True
                self.retry = None
            return True, []
        if event_type == "session.idle":
            self.idle = True
            return True, []
        if event_type == "session.error":
            message = _error_text(props.get("error"))
            if message:
                self.error = message
            return True, []
        if event_type == "permission.asked":
            return True, self._permission_notice(props)
        return True, []

    def _permission_notice(self, props: dict[str, Any]) -> list[AgentEvent]:
        action = str(props.get("permission") or "a tool")
        patterns = tuple(str(p) for p in props.get("patterns") or [])
        key = (action, patterns)
        if key in self._permissions:
            return []
        first = not self._permissions
        self._permissions.add(key)
        shown = ", ".join(f"`{p}`" for p in patterns[:3])
        if len(patterns) > 3:
            shown += f" and {len(patterns) - 3} more"
        line = f"Waiting for permission: `{action}`" + (f" on {shown}" if shown else "")
        if first:
            line += (
                ". Approve it in the OpenCode app or TUI, or allow it in your OpenCode "
                "agent permissions"
            )
        return [AgentTextDelta(f"\n\n⏸ {line}.\n\n" if first else f"\n⏸ {line}.\n")]

    def replay(self, messages: list[dict[str, Any]]) -> list[AgentEvent]:
        """Feed this turn's messages from history; emits only what was missed."""
        out: list[AgentEvent] = []
        infos = [m.get("info") for m in messages if isinstance(m.get("info"), dict)]
        for info in infos:
            self._note_message(info)
        if self.user_message_id is None:
            users = [i for i in infos if i.get("role") == "user" and self._is_turn_message(i)]
            if users:
                self.user_message_id = users[-1].get("id")
        for message in messages:
            info = message.get("info") if isinstance(message.get("info"), dict) else {}
            if info.get("role") != "assistant":
                continue
            if self.user_message_id is not None:
                if info.get("parentID") != self.user_message_id:
                    continue
            elif not self._is_turn_message(info):
                continue
            self.started = True
            for part in message.get("parts") or []:
                if isinstance(part, dict):
                    out.extend(self._part(part))
            message_error = _error_text(info.get("error")) if info.get("error") else None
            if message_error:
                self.error = message_error
        return out


def parse_line(line: str) -> dict[str, Any] | None:
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line or line.startswith(":"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


# ── The event stream ──────────────────────────────────────────────────────


async def _stream(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    queue: asyncio.Queue,
    path_hint: list[str],
) -> None:
    """Pump one SSE connection into ``queue`` as ("open"|"line"|"closed"|"fatal", value)."""
    paths = list(path_hint) or list(EVENT_PATHS)
    last_error: BaseException | None = None
    for path in paths:
        opened = False
        try:
            async with client.stream(
                "GET",
                f"/{path}",
                headers={**headers, "Accept": "text/event-stream"},
                timeout=httpx.Timeout(None, connect=5),
            ) as response:
                if response.status_code == 401:
                    raise auth_error(bool(headers.get("Authorization")))
                content_type = response.headers.get("content-type", "")
                if response.status_code == 404 or "event-stream" not in content_type:
                    last_error = ConnectionError(f"/{path} is not an event stream")
                    continue
                response.raise_for_status()
                path_hint[:] = [path]
                opened = True
                await queue.put(("open", None))
                async for line in response.aiter_lines():
                    if line:
                        await queue.put(("line", line))
            await queue.put(("closed", ConnectionError("OpenCode closed the event stream")))
            return
        except asyncio.CancelledError:
            raise
        except OpenCodeAuthError as exc:
            await queue.put(("fatal", exc))
            return
        except Exception as exc:  # noqa: BLE001 - reported for reconnect.
            last_error = exc
            if opened:
                await queue.put(("closed", exc))
                return
    await queue.put(("closed", last_error or ConnectionError("no OpenCode event stream")))


# ── One turn ──────────────────────────────────────────────────────────────


async def run_turn(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    session_id: str,
    send_prompt: Callable[[], Awaitable[None]],
    *,
    attached: bool = False,
) -> AsyncIterator[AgentEvent]:
    """Subscribe, send the prompt once, and follow the session until idle.

    ``attached=True`` follows work that is already running (no prompt of
    ours): it counts as started at once, and nothing is replayed from before.
    """
    loop = asyncio.get_running_loop()
    now = loop.time
    translator = Translator(session_id)
    queue: asyncio.Queue = asyncio.Queue()
    path_hint: list[str] = []

    def start_stream() -> asyncio.Task:
        return asyncio.create_task(_stream(client, headers, queue, path_hint))

    async def stop(task: asyncio.Task) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    stream_task = start_stream()
    stream_open = False
    try:
        # Subscribe before prompting so the first events aren't missed. If the
        # stream isn't up yet, carry on: reconnect + replay will catch up.
        deadline = now() + SUBSCRIBE_WAIT_SECONDS
        early: list[tuple[str, Any]] = []
        while now() < deadline:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=deadline - now())
            except asyncio.TimeoutError:
                break
            if kind == "fatal":
                raise value
            if kind == "open":
                stream_open = True
                break
            early.append((kind, value))
            if kind == "closed":
                break
        for item in early:
            queue.put_nowait(item)

        translator.sent_ms = time.time() * 1000
        await send_prompt()
        sent_at = now()
        if attached:
            translator.started = True

        stream_seen = now()
        last_activity = now()
        last_activity_wall = time.time()
        server_ok_at = now()
        last_poll = 0.0
        reconnects = 0
        reconnect_at: float | None = None
        gap = not stream_open
        unreachable = False
        reported: tuple[str, int | None] | None = None
        last_report = 0.0
        last_error: str | None = None

        async def catch_up() -> list[AgentEvent]:
            try:
                messages = await asyncio.wait_for(
                    recent_messages(client, headers, session_id), timeout=15
                )
            except OpenCodeAuthError:
                raise
            except Exception:  # noqa: BLE001 - best effort; live events continue.
                return []
            return translator.replay(messages)

        def finished() -> bool:
            return translator.idle and (
                translator.started or now() - sent_at > START_GRACE_SECONDS
            )

        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                kind, value = None, None
            t = now()

            if kind == "line":
                stream_seen = t
                reconnects = 0  # data is flowing again: the backoff starts over
                event = parse_line(value)
                if event is not None:
                    ours, events = translator.translate(event)
                    if ours:
                        last_activity, last_activity_wall = t, time.time()
                        unreachable = False
                    for item in events:
                        yield item
                    if finished():
                        if gap:
                            for item in await catch_up():
                                yield item
                        break
            elif kind == "open":
                stream_open, stream_seen = True, t
                reconnect_at, last_error = None, None
                if gap:
                    # Events published while we weren't subscribed are only
                    # in the session history: replay this turn's messages.
                    recovered = await catch_up()
                    if recovered:
                        last_activity, last_activity_wall = now(), time.time()
                    for item in recovered:
                        yield item
                    gap = False
            elif kind == "closed":
                stream_open, gap = False, True
                last_error = str(value)[:200] if value else None
                if reconnects >= MAX_RECONNECTS:
                    raise OpenCodeError(
                        f"Lost the OpenCode event stream and could not reconnect after "
                        f"{reconnects} attempts ({last_error or 'connection closed'})."
                    )
                delay = RECONNECT_DELAYS[min(reconnects, len(RECONNECT_DELAYS) - 1)]
                reconnects += 1
                reconnect_at = t + delay
            elif kind == "fatal":
                raise value

            if reconnect_at is not None and t >= reconnect_at and stream_task.done():
                reconnect_at = None
                stream_task = start_stream()

            if stream_open and t - stream_seen > STREAM_STALE_SECONDS:
                # Connected but silent, not even heartbeats: half-open socket.
                await stop(stream_task)
                queue.put_nowait(
                    ("closed", ConnectionError(f"no heartbeat for {int(t - stream_seen)}s"))
                )
                stream_open = False

            quiet = t - last_activity >= STATUS_POLL_SECONDS
            if t - last_poll >= STATUS_POLL_SECONDS and (quiet or not stream_open):
                last_poll = t
                try:
                    status = await asyncio.wait_for(
                        session_status(client, headers, session_id), timeout=5
                    )
                except OpenCodeAuthError:
                    raise
                except Exception as exc:  # noqa: BLE001 - transient until proven otherwise.
                    unreachable = True
                    if t - server_ok_at > UNREACHABLE_SECONDS:
                        raise OpenCodeError(
                            f"OpenCode has not answered for {int(t - server_ok_at)} seconds "
                            f"({str(exc)[:120] or type(exc).__name__}). The OpenCode session may "
                            "still be running; send a message to continue it."
                        ) from exc
                else:
                    server_ok_at, unreachable = t, False
                    if status is None:
                        translator.idle = True
                        if finished():
                            for item in await catch_up():
                                yield item
                            break
                    else:
                        translator.idle = False
                        translator.started = True
                        translator.retry = status if status.get("type") == "retry" else None

            # Liveness for the progress UI: on change, and periodically while
            # nothing else is arriving (so "last activity" stays current).
            if unreachable:
                state, detail = "unreachable", "OpenCode is not answering"
            elif not stream_open:
                state, detail = "reconnecting", last_error
            elif translator.retry:
                retry = translator.retry
                state = "retrying"
                detail = str(retry.get("message") or "")[:200] or None
            elif t - last_activity >= WAITING_AFTER_SECONDS:
                state, detail = "waiting", None
            else:
                state, detail = "connected", None
            key = (state, reconnects if state == "reconnecting" else None)
            if key != reported or (
                state != "connected" and t - last_report >= STATUS_REPORT_SECONDS
            ):
                reported, last_report = key, t
                attempt = translator.retry.get("attempt") if state == "retrying" else None
                yield AgentStatus(
                    state=state,
                    last_activity=last_activity_wall,
                    detail=detail,
                    attempt=reconnects if state == "reconnecting" else attempt,
                )

        # Lines already queued when the turn ended.
        while not queue.empty():
            kind, value = queue.get_nowait()
            if kind == "line" and (event := parse_line(value)) is not None:
                for item in translator.translate(event)[1]:
                    yield item
        if translator.error:
            raise OpenCodeError(translator.error)
    finally:
        await stop(stream_task)
