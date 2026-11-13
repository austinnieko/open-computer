"""Compaction summaries: cut-off detection, retry with a larger budget, and finish_reason from each provider."""

import asyncio
import json
import unittest
from unittest import mock

import httpx

from cptr.utils import ai, summarize
from cptr.utils.ai import CompletionText

MSGS = [{"role": "user", "content": "fix the bug"}, {"role": "assistant", "content": "edited app/main.py"}]


def run(coro):
    return asyncio.run(coro)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch("cptr.utils.utility_models.configured_utility_model", mock.AsyncMock(return_value=None))
        p.start()
        self.addCleanup(p.stop)

    def summarize_with(self, *replies):
        gen = mock.AsyncMock(side_effect=list(replies))
        with mock.patch.object(ai, "generate_text", gen):
            out = run(summarize.summarize_messages(MSGS, None, {"provider": "openai"}, "m"))
        return out, gen

    def test_a_complete_summary_is_used_with_the_larger_budget(self):
        out, gen = self.summarize_with(CompletionText.of("- edited app/main.py", "stop"))
        self.assertEqual(out, "- edited app/main.py")
        self.assertEqual(gen.await_args.kwargs["max_tokens"], summarize.SUMMARY_MAX_TOKENS)
        self.assertGreaterEqual(summarize.SUMMARY_MAX_TOKENS, 4000)

    def test_a_cut_off_summary_is_retried_with_a_bigger_budget(self):
        out, gen = self.summarize_with(CompletionText.of("- partial", "length"), CompletionText.of("- full", "stop"))
        self.assertEqual(out, "- full")
        self.assertEqual([c.kwargs["max_tokens"] for c in gen.await_args_list],
                         [summarize.SUMMARY_MAX_TOKENS, summarize.SUMMARY_RETRY_MAX_TOKENS])
        self.assertIn("more concise", gen.await_args_list[1].kwargs["system"])

    def test_a_summary_cut_off_twice_is_kept_with_a_visible_note(self):
        out, _ = self.summarize_with(CompletionText.of("- a", "length"), CompletionText.of("- longer", "length"))
        self.assertTrue(out.startswith("- longer"))
        self.assertIn("cut off at its length limit", out)

    def test_an_empty_reply_falls_back_instead_of_saving_nothing(self):
        out, _ = self.summarize_with(CompletionText.of("", "length"), CompletionText.of("", "length"))
        self.assertIn("fix the bug", out)  # the naive fallback, not an empty summary

    def test_plain_strings_without_a_finish_reason_still_work(self):
        out, gen = self.summarize_with("- old-style reply")
        self.assertEqual(out, "- old-style reply")
        self.assertEqual(gen.await_count, 1)

    def test_long_tool_output_keeps_more_than_before(self):
        big = "x" * 50_000
        gen = mock.AsyncMock(return_value=CompletionText.of("ok", "stop"))
        with mock.patch.object(ai, "generate_text", gen):
            run(summarize.summarize_messages([{"role": "tool", "content": big}], None, {}, "m"))
        sent = gen.await_args.kwargs["messages"][0]["content"]
        self.assertGreater(sent.count("x"), 3000)  # was capped near 1,500 characters per message
        self.assertLessEqual(len(sent), summarize.TRANSCRIPT_CHAR_LIMIT + 50)


class FinishReasonTests(unittest.TestCase):
    def complete(self, provider, api_type, payload):
        real = httpx.AsyncClient

        def client(*a, **kw):
            return real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))

        with mock.patch.object(ai.httpx, "AsyncClient", client):
            return run(ai.chat_completion(provider=provider, base_url="http://x", api_key="", model="m",
                                          messages=[{"role": "user", "content": "hi"}], api_type=api_type))

    def test_chat_completions_length(self):
        out = self.complete("openai", "chat_completions",
                            {"choices": [{"message": {"content": "abc"}, "finish_reason": "length"}]})
        self.assertEqual((str(out), out.finish_reason), ("abc", "length"))
        self.assertIsInstance(out, str)

    def test_anthropic_max_tokens_is_length(self):
        out = self.complete("anthropic", "messages", {"content": [{"text": "abc"}], "stop_reason": "max_tokens"})
        self.assertEqual(out.finish_reason, "length")
        out = self.complete("anthropic", "messages", {"content": [{"text": "abc"}], "stop_reason": "end_turn"})
        self.assertEqual(out.finish_reason, "stop")

    def test_responses_incomplete_is_length(self):
        out = self.complete("openai", "responses", {
            "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "abc"}]}]})
        self.assertEqual((str(out), out.finish_reason), ("abc", "length"))

    def test_null_content_becomes_empty_text(self):
        out = self.complete("openai", "chat_completions",
                            {"choices": [{"message": {"content": None}, "finish_reason": "length"}]})
        self.assertEqual(str(out), "")
        self.assertEqual(json.dumps({"t": out}), '{"t": ""}')


if __name__ == "__main__":
    unittest.main()
