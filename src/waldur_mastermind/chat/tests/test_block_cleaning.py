"""Unit tests for the user-facing answer cleaners in block_schemas:
stripping text-form tool-call artifacts and dropping transient narration."""

from django.test import SimpleTestCase

from waldur_mastermind.chat.block_schemas import (
    clean_answer_blocks,
    strip_tool_call_artifacts,
)


class StripToolCallArtifactsTest(SimpleTestCase):
    def test_strips_full_text_tool_call_block(self):
        # The exact shape a model leaked instead of using native calling.
        text = (
            "<tool_call>\n"
            "<function=explain_project_credit_balance>\n"
            "<parameter=project_name>\n"
            "Crestford Helios\n"
            "</parameter>\n"
            "</function>\n"
            "</tool_call>"
        )
        self.assertEqual(strip_tool_call_artifacts(text), "")

    def test_strips_artifact_but_keeps_surrounding_prose(self):
        text = "Here is the balance.\n<tool_call><function=x></function></tool_call>"
        self.assertEqual(strip_tool_call_artifacts(text), "Here is the balance.")

    def test_strips_stray_tags(self):
        self.assertEqual(strip_tool_call_artifacts("a </function> b"), "a  b")

    def test_leaves_plain_text_untouched(self):
        self.assertEqual(
            strip_tool_call_artifacts("Project Alpha has 1,000 credit (a < b)."),
            "Project Alpha has 1,000 credit (a < b).",
        )

    def test_empty_input(self):
        self.assertEqual(strip_tool_call_artifacts(""), "")


class CleanAnswerBlocksTest(SimpleTestCase):
    def test_keeps_tool_blocks(self):
        blocks = [
            {"id": "blk_0", "key": "tool", "tool": {"name": "search_tools"}},
            {"id": "blk_1", "key": "markdown", "content": "Done."},
        ]
        cleaned = clean_answer_blocks(blocks)
        self.assertEqual([b["key"] for b in cleaned], ["tool", "markdown"])

    def test_strips_artifact_and_drops_now_empty_block(self):
        blocks = [
            {
                "id": "blk_0",
                "key": "markdown",
                "content": "<tool_call><function=x></function></tool_call>",
            },
            {"id": "blk_1", "key": "markdown", "content": "Real answer."},
        ]
        cleaned = clean_answer_blocks(blocks)
        self.assertEqual([b["content"] for b in cleaned], ["Real answer."])

    def test_leaves_code_block_content_untouched(self):
        # An angle-bracket inside a code block is intentional, not an artifact.
        blocks = [
            {
                "id": "blk_0",
                "key": "code",
                "tag": "html",
                "content": "<function>foo()</function>",
            },
        ]
        cleaned = clean_answer_blocks(blocks)
        self.assertEqual(cleaned[0]["content"], "<function>foo()</function>")

    def test_handles_none_and_empty(self):
        self.assertEqual(clean_answer_blocks(None), [])
        self.assertEqual(clean_answer_blocks([]), [])
