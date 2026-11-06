"""Streamed reasoning goes to the browser as appended pieces, not whole items.

Run: uv run python -m unittest tests.test_reasoning_stream -v
"""

from __future__ import annotations

import json
import unittest

from cptr.utils.chat_task import ReasoningStream


def item(text, status="in_progress", rid="r1", block="reasoning_text"):
    return {
        "type": "reasoning",
        "id": rid,
        "status": status,
        "content": [{"type": block, "text": text}],
    }


class ReasoningStreamTests(unittest.TestCase):
    def test_first_update_is_full_then_only_new_text(self):
        s = ReasoningStream()
        first = s.payload({"output": item("Let me")})
        self.assertEqual(first["output"]["content"][0]["text"], "Let me")
        nxt = s.payload({"output": item("Let me think"), "extra": 1})
        self.assertNotIn("output", nxt)
        self.assertEqual(nxt["reasoning_delta"], {"id": "r1", "offset": 6, "text": " think"})
        self.assertEqual(nxt["extra"], 1)

    def test_completion_and_rewrites_send_the_full_item(self):
        s = ReasoningStream()
        s.payload({"output": item("abc")})
        self.assertIn("output", s.payload({"output": item("xyz")}))  # not a continuation
        self.assertIn("reasoning_delta", s.payload({"output": item("xyz!")}))
        done = s.payload({"output": item("xyz!", status="completed")})
        self.assertEqual(done["output"]["status"], "completed")

    def test_no_new_text_sends_nothing(self):
        s = ReasoningStream()
        s.payload({"output": item("abc")})
        self.assertIsNone(s.payload({"output": item("abc")}))

    def test_other_events_and_unusual_items_pass_through(self):
        s = ReasoningStream()
        for data in (
            {"delta": "hi"},
            {"output": {"type": "function_call", "call_id": "c"}},
            {"output": {"type": "reasoning", "status": "in_progress", "content": []}},  # no id
            {"output": {**item("a"), "summary": [{"type": "summary_text", "text": "s"}]}},
            {"output": {**item("a"), "content": [{"text": "a"}, {"text": "b"}]}},
        ):
            self.assertIs(s.payload(data), data)
            self.assertIs(s.payload(data), data)

    def test_items_are_tracked_separately(self):
        s = ReasoningStream()
        s.payload({"output": item("a", rid="r1")})
        s.payload({"output": item("b", rid="r2")})
        self.assertEqual(
            s.payload({"output": item("ab", rid="r1")})["reasoning_delta"]["offset"], 1
        )
        self.assertEqual(
            s.payload({"output": item("bc", rid="r2")})["reasoning_delta"]["text"], "c"
        )

    def test_traffic_grows_linearly(self):
        think = "The handler reads the session before the token. " * 300  # ~15k chars
        s = ReasoningStream()
        sent = sum(
            len(json.dumps(s.payload({"output": item(think[:i])}) or {}))
            for i in range(4, len(think) + 1, 4)
        )
        whole = sum(
            len(json.dumps({"output": item(think[:i])})) for i in range(4, len(think) + 1, 4)
        )
        self.assertLess(sent, 400_000)
        self.assertGreater(whole, 25_000_000)


if __name__ == "__main__":
    unittest.main()
