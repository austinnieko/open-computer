"""Notice when the model keeps making the same tool call and getting the same answer.

A model polling a log that has stopped changing (``sleep 55; tail log``) can loop for hours: every
call succeeds, so nothing ends the reply. The guard keys each call by tool name and arguments and
remembers the last result for that key. When the same call returns the same result again, a note is
added to what the model sees; after ``CHAT_REPEAT_LIMIT`` identical results the call is no longer run
and the model is told to change approach or report back.
"""

from __future__ import annotations

import hashlib
import json
import os

WARN_AFTER = 3
LIMIT = max(WARN_AFTER + 1, int(os.environ.get("CHAT_REPEAT_LIMIT", "8") or 8))

# Fields that differ on every run of the same command even when nothing else changed.
_VOLATILE_KEYS = {"id", "log_path", "next_offset", "truncated", "started_at", "finished_at",
                  "created_at", "duration", "duration_ms", "elapsed", "elapsed_ms"}


def _key(name: str, args) -> str:
    try:
        text = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(args)
    return name + "\0" + text


def _normalize(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _VOLATILE_KEYS:
                continue
            if k == "output" and isinstance(v, list) and all(isinstance(c, dict) for c in v):
                # Process output arrives in chunks that split differently from run to run.
                v = "".join(str(c.get("data", "")) for c in v)
            out[k] = _normalize(v)
        return out
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def _fingerprint(result) -> str:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            pass
    try:
        text = json.dumps(_normalize(result), sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(result)
    return hashlib.sha256(text.encode()).hexdigest()


class RepeatGuard:
    def __init__(self, warn_after: int = WARN_AFTER, limit: int = LIMIT):
        self.warn_after = warn_after
        self.limit = limit
        self._last: dict[str, tuple[str, int]] = {}  # key -> (fingerprint, identical results in a row)

    def blocked(self, name: str, args) -> str | None:
        """The text to return instead of running the call, or None to run it."""
        key = _key(name, args)
        seen = self._last.get(key)
        if not seen or seen[1] < self.limit:
            return None
        # Let the next attempt run, so a call can succeed again once things have changed; if it
        # still returns the same result it is refused again.
        self._last[key] = (seen[0], self.limit - 1)
        return (
            f"Error: not run. `{name}` was called {seen[1]} times with these exact arguments and "
            "returned the same result every time, so nothing is changing. Stop repeating it: check "
            "whether the process you are waiting for is still alive, try a different command, or "
            "tell the user what is stuck."
        )

    def record(self, name: str, args, result):
        """Remember the result; returns it, with a note appended once it has repeated."""
        key = _key(name, args)
        fp = _fingerprint(result)
        prev = self._last.get(key)
        count = prev[1] + 1 if prev and prev[0] == fp else 1
        self._last[key] = (fp, count)
        if count < self.warn_after or not isinstance(result, str):
            return result
        return (
            f"{result}\n\n[Note: this exact call has now returned the same result {count} times in a "
            "row. The output is not changing. Waiting longer will not help; inspect the process (is "
            "it still running?) or change approach. After "
            f"{self.limit} identical results the call will be refused.]"
        )
