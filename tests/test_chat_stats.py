import json
import unittest
from types import SimpleNamespace

from cptr.routers.chat import _tally_messages, _usage_numbers
from cptr.utils.agents.opencode_v2 import EventTranslator


def msg(role, **kw):
    base = dict(id="m", role=role, model=None, usage=None, output=None, done=True, created_at=1)
    base.update(kw)
    return SimpleNamespace(**base)


class UsageNumbersTests(unittest.TestCase):
    def test_api_usage(self):
        n = _usage_numbers(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 40},
                "duration_ms": 1500,
            }
        )
        self.assertEqual((n["input"], n["output"], n["cached"], n["total"]), (100, 20, 40, 120))
        self.assertEqual(n["duration_ms"], 1500)

    def test_agent_turn_sums_win(self):
        n = _usage_numbers(
            {
                "input_tokens": 5,
                "output_tokens": 1,
                "turn": {"input_tokens": 50, "output_tokens": 9, "cost_usd": 0.02},
            }
        )
        self.assertEqual((n["input"], n["output"], n["total"], n["cost_usd"]), (50, 9, 59, 0.02))

    def test_empty(self):
        self.assertEqual(_usage_numbers(None)["total"], 0)


class TallyTests(unittest.TestCase):
    def test_tools_files_models(self):
        out = [
            {
                "type": "function_call",
                "name": "edit_file",
                "arguments": json.dumps({"path": "/ws/a.py"}),
            },
            {
                "type": "function_call",
                "name": "agent_tool",
                "status": "failed",
                "arguments": {"title": "write", "input": {"filePath": "/ws/b.md"}},
            },
            {"type": "function_call", "name": "shell", "arguments": "{}"},
            {"type": "message"},
        ]
        stats = _tally_messages(
            [
                msg("user"),
                msg(
                    "assistant",
                    model="m1",
                    output=out,
                    usage={"total_tokens": 10, "duration_ms": 2000},
                ),
                msg("user"),
                msg("assistant", model="m1", usage={"total_tokens": 5}),
            ],
            "/ws/",
        )
        self.assertEqual(stats["user_messages"], 2)
        self.assertEqual(stats["totals"]["total"], 15)
        self.assertEqual(stats["totals"]["duration_ms"], 2000)
        self.assertEqual({f["path"] for f in stats["files"]}, {"a.py", "b.md"})
        self.assertEqual(stats["models"], [{"id": "m1", "replies": 2}])
        write = next(t for t in stats["tools"] if t["name"] == "write")
        self.assertEqual(write["failed"], 1)
        self.assertEqual(stats["turns"][0]["tool_calls"], 3)


class TranslatorUsageTests(unittest.TestCase):
    def step(self, inp, out, cost):
        return {
            "type": "session.step.ended",
            "data": {
                "sessionID": "s",
                "tokens": {
                    "input": inp,
                    "output": out,
                    "reasoning": 1,
                    "cache": {"read": 2, "write": 0},
                },
                "cost": cost,
            },
        }

    def test_last_step_and_turn_sums(self):
        t = EventTranslator("s")
        self.assertEqual(t.translate(self.step(100, 10, 0.01)), [])
        t.translate(self.step(150, 20, 0.02))
        self.assertEqual(t.usage["input_tokens"], 150)
        self.assertEqual(t.usage["total_tokens"], 170)
        turn = t.usage["turn"]
        self.assertEqual((turn["input_tokens"], turn["output_tokens"], turn["steps"]), (250, 30, 2))
        self.assertAlmostEqual(turn["cost_usd"], 0.03)
        self.assertEqual(turn["cache_read_input_tokens"], 4)

    def test_other_session_ignored(self):
        t = EventTranslator("other")
        t.translate(self.step(100, 10, 0.01))
        self.assertEqual(t.usage, {})


if __name__ == "__main__":
    unittest.main()
