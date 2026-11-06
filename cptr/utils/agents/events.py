"""Normalized event types emitted by coding agent adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentTextDelta:
    text: str


@dataclass
class AgentReasoningDelta:
    text: str


@dataclass
class AgentToolUpdate:
    call_id: str
    status: str
    name: str | None = None
    arguments: dict[str, Any] | None = None
    output: str | None = None


@dataclass
class AgentToolOutputDelta:
    call_id: str
    delta: str
    stream_kind: str = "tool_output"
    replace: bool = False


@dataclass
class AgentDone:
    usage: dict[str, Any] | None = None
    resume_state: dict[str, Any] | None = None


@dataclass
class AgentError:
    message: str
    # Set when the agent's own session survives the failure (for example the
    # connection to it was lost), so the next message continues that session
    # instead of starting over.
    resume_state: dict[str, Any] | None = None


@dataclass
class AgentSessionStarted:
    """The agent's own session exists: save it now, before any work happens.

    Without this the session id is only known at the end of the turn, so a
    restart mid-turn makes the next message open a second session while the
    first one is still working in the same folder.
    """

    resume_state: dict[str, Any]


@dataclass
class AgentStatus:
    """Connection and liveness of the agent run, for the progress UI.

    Never content: no text, no reasoning. ``state`` is one of
    ``connected`` (events flowing), ``waiting`` (the agent reports it is busy
    but has sent nothing new), ``retrying`` (the agent is retrying its model
    call), ``reconnecting`` (the event stream dropped; recovering) or
    ``unreachable`` (the agent server is not answering).
    """

    state: str
    last_activity: float | None = None  # epoch seconds of the last real event
    detail: str | None = None
    attempt: int | None = None


AgentEvent = (
    AgentTextDelta
    | AgentReasoningDelta
    | AgentToolUpdate
    | AgentToolOutputDelta
    | AgentDone
    | AgentError
    | AgentSessionStarted
    | AgentStatus
)
