"""Chat title extraction: JSON, thinking-model output, plain text, fallback."""

import unittest

from cptr.utils.chat_task import heuristic_title, title_from_response


class TitleTests(unittest.TestCase):
    def test_json(self):
        self.assertEqual(title_from_response('{"title": "Fix Login Bug"}'), "Fix Login Bug")

    def test_thinking_model_output(self):
        text = (
            '<think>The user wants a title about schemas.</think>\n{"title": "Bus Schema Review"}'
        )
        self.assertEqual(title_from_response(text), "Bus Schema Review")

    def test_unclosed_think_block_is_ignored(self):
        self.assertEqual(title_from_response("<think>still reasoning about the"), "")

    def test_plain_text_reply(self):
        self.assertEqual(
            title_from_response('Title: "Motion Service Contract Check"'),
            "Motion Service Contract Check",
        )

    def test_fenced_json(self):
        self.assertEqual(
            title_from_response('```json\n{"title": "Deploy Notes"}\n```'), "Deploy Notes"
        )

    def test_heuristic_strips_markdown(self):
        self.assertEqual(
            heuristic_title("## I had an agent working on the below. I need you to check it"),
            "I had an agent working on the below. I need you to check it",
        )

    def test_heuristic_skips_blank_lines_and_truncates(self):
        title = heuristic_title("\n\n> " + "word " * 40)
        self.assertTrue(title.endswith("…"))
        self.assertLessEqual(len(title), 61)


if __name__ == "__main__":
    unittest.main()
