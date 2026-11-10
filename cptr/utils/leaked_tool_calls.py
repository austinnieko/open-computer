"""Recover tool calls that a model wrote as plain text.

Some servers (e.g. tensorfold serving GLM) reject a tool call whose name was
not offered and pass it through as text, e.g.::

    <tool_call>ot_replace_file<arg_key>path</arg_key><arg_value>/x</arg_value></tool_call>

Left alone, the chat loop sees "no tool calls" and ends the reply mid-task.
``extract_leaked_tool_call`` finds such a call at the end of the text so the
loop can answer it with a tool error and let the model correct itself.
"""

from __future__ import annotations

import json
import re

_START = re.compile(r"<tool_call>")
_NAME = re.compile(r"\s*([A-Za-z_][\w.\-]*)")
_ARG = re.compile(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", re.DOTALL)


def _value(raw: str):
    raw = raw.strip()
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def extract_leaked_tool_call(text: str) -> tuple[int, str, dict] | None:
    """Return (start_index, name, arguments) for a trailing leaked call, else None.

    Only a call that ends the text counts (nothing but whitespace after
    ``</tool_call>``, or the text simply stops inside it), so prose that merely
    mentions the syntax is left alone.
    """
    if not text or "<tool_call>" not in text:
        return None
    start = None
    for m in _START.finditer(text):
        start = m.start()
    if start is None:
        return None
    body = text[start + len("<tool_call>") :]
    end = body.find("</tool_call>")
    if end != -1:
        if body[end + len("</tool_call>") :].strip():
            return None
        body = body[:end]

    stripped = body.strip()
    # Hermes/JSON style: <tool_call>{"name": ..., "arguments": {...}}</tool_call>
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError:
            return None
        name = obj.get("name") if isinstance(obj, dict) else None
        args = obj.get("arguments", {}) if isinstance(obj, dict) else {}
        if isinstance(args, str):
            args = _value(args)
        if not isinstance(name, str) or not name:
            return None
        return start, name, args if isinstance(args, dict) else {"value": args}

    m = _NAME.match(body)
    if not m:
        return None
    rest = body[m.end() :]
    if end == -1 and "<arg_" in _ARG.sub("", rest):
        # Cut off mid-argument: don't run a call with half its arguments.
        return None
    args = {k.strip(): _value(v) for k, v in _ARG.findall(rest)}
    return start, m.group(1), args
