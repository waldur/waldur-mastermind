import json
import unittest
from unittest.mock import Mock, patch

import requests
from rest_framework import test as drf_test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import TokenQuota
from waldur_mastermind.chat.ui_registry import ui_registry  # noqa: F401
from waldur_mastermind.chat.views import LLMStreamer

"""
NDJSON streaming response format for chat messages.

Uses single-character keys for bandwidth optimization. Each line is a JSON object
containing one or more of these fields:

- k: Component key (markdown, code, table, mermaid, load)
- c: Content payload (text)
- t: Type/tag (language for code blocks, component for loading)
- h: Table headers (array of strings)
- r: Table rows (array of arrays)
- n: Row count (number)
- m: Metadata (object with additional info like token counts)
- e: Error message (string)

Examples:
    {"k":"markdown","c":"Hello!"}
    {"k":"code","c":"print('hi')","t":"python"}
    {"k":"table","h":["Name","State"],"r":[["VM1","OK"]],"n":1}
    {"m":{"tokens":150}}
    {"e":"Request failed"}
"""


class LLMStreamerTest(unittest.TestCase):
    def test_streamer_parses_content(self):
        upstream_payload = {"content": "Hello", "additional_kwargs": {}}
        fake_stream = ["data: " + json.dumps(upstream_payload)]

        mock_response = Mock()
        mock_response.iter_lines.return_value = fake_stream
        mock_response.raise_for_status = Mock()

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = mock_response

            streamer = LLMStreamer("input", "https://example.com/stream", "dummy-token")
            chunks = list(streamer)

            found_hello = False
            for chunk in chunks:
                data = json.loads(chunk)
                if data.get("k") == "markdown" and data.get("c") == "Hello":
                    found_hello = True

            self.assertTrue(
                found_hello, f"Did not find 'Hello' UI event in chunks: {chunks}"
            )

    def test_streamer_parses_code_block(self):
        fake_stream = [
            "data: " + json.dumps({"content": "```python\n"}),
            "data: " + json.dumps({"content": "def hello():\n"}),
            "data: " + json.dumps({"content": "    print('world')\n"}),
            "data: " + json.dumps({"content": "```"}),
        ]

        mock_response = Mock()
        mock_response.iter_lines.return_value = fake_stream
        mock_response.raise_for_status = Mock()

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = mock_response

            streamer = LLMStreamer("input", "https://example.com/stream", "dummy-token")
            chunks = list(streamer)

            found_loading = False
            found_code = False
            expected_content = "def hello():\n    print('world')\n"

            for chunk in chunks:
                data = json.loads(chunk)
                if data.get("k") == "load" and data.get("t") == "code":
                    found_loading = True
                if (
                    data.get("k") == "code"
                    and data.get("c") == expected_content
                    and data.get("t") == "python"
                ):
                    found_code = True

            self.assertTrue(
                found_loading,
                f"Did not find Loading event with tag 'python'. Chunks: {chunks}",
            )
            self.assertTrue(
                found_code,
                f"Did not find Code event with correct content. Chunks: {chunks}",
            )

    def test_streamer_parses_split_delimiter(self):
        fake_stream = [
            "data: " + json.dumps({"content": "Here is code ``"}),
            "data: " + json.dumps({"content": "`python\nprint('split')\n```"}),
        ]

        mock_response = Mock()
        mock_response.iter_lines.return_value = fake_stream
        mock_response.raise_for_status = Mock()

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = mock_response

            streamer = LLMStreamer("input", "https://example.com/stream", "dummy-token")
            chunks = list(streamer)

            found_code = False
            expected_content = "print('split')\n"
            for chunk in chunks:
                data = json.loads(chunk)
                if (
                    data.get("k") == "code"
                    and data.get("c") == expected_content
                    and data.get("t") == "python"
                ):
                    found_code = True

            self.assertTrue(
                found_code, "Did not find Code UI event with split delimiter"
            )

    def test_streamer_yields_loading_status(self):
        fake_stream = [
            "data: " + json.dumps({"content": "Here is some code:\n```python\n"}),
        ]

        mock_response = Mock()
        mock_response.iter_lines.return_value = fake_stream
        mock_response.raise_for_status = Mock()

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = mock_response

            streamer = LLMStreamer("input", "https://example.com/stream", "dummy-token")
            chunks = list(streamer)

            found_loading = False
            for chunk in chunks:
                data = json.loads(chunk)
                if data.get("k") == "load" and data.get("t") == "code":
                    found_loading = True

            self.assertTrue(found_loading, "Did not find Loading UI event")

    def test_streamer_parses_mermaid_block(self):
        fake_stream = [
            "data: "
            + json.dumps(
                {"content": "Here is a diagram:\n```mermaid\ngraph TD\nA-->B\n```"}
            ),
        ]

        mock_response = Mock()
        mock_response.iter_lines.return_value = fake_stream
        mock_response.raise_for_status = Mock()

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = mock_response

            streamer = LLMStreamer("input", "https://example.com/stream", "dummy-token")
            chunks = list(streamer)

            found_mermaid = False
            expected_content = "graph TD\nA-->B\n"
            for chunk in chunks:
                data = json.loads(chunk)
                if data.get("k") == "mermaid" and data.get("c") == expected_content:
                    found_mermaid = True

            self.assertTrue(found_mermaid, "Did not find Mermaid UI event")

    def test_streamer_complex_stream(self):
        """
        Test a complex stream sequence with edge cases:
        1. Markdown text
        2. Split-token Code block with special chars (```py + thon)
        3. Markdown with leading/trailing whitespace
        4. Split-token Mermaid block (```mer + maid)
        5. Consecutive code blocks (empty then with content)
        6. Code with newlines, quotes, and escapes
        7. Final markdown
        """
        fake_stream = [
            "data: " + json.dumps({"content": "Start text. "}),
            "data: " + json.dumps({"content": "Here is code: ```py"}),
            "data: "
            + json.dumps(
                {"content": "thon\nprint('hello\\'world')\nprint(\"quoted\")\n"}
            ),
            "data: " + json.dumps({"content": "```\n   Middle text with spacing.   "}),
            "data: " + json.dumps({"content": "Diagram: ```mer"}),
            "data: " + json.dumps({"content": "maid\ngraph TD\nA-->B\nC--msg-->D\n"}),
            "data: " + json.dumps({"content": "```\nEmpty code: ```javascript"}),
            "data: " + json.dumps({"content": "\n```\n"}),
            "data: "
            + json.dumps(
                {
                    "content": "Final: ```bash\necho \"Hello 世界\"\necho 'test\\\\path'\n"
                }
            ),
            "data: " + json.dumps({"content": "```\nEnd text with special: @#$%."}),
        ]

        mock_response = Mock()
        mock_response.iter_lines.return_value = fake_stream
        mock_response.raise_for_status = Mock()

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = mock_response

            streamer = LLMStreamer("input", "https://example.com/stream", "dummy-token")
            chunks = list(streamer)
            events = [json.loads(chunk) for chunk in chunks]

            def find_event_index(key, val_type=None, content=None):
                """Find first event matching criteria, return index or -1."""
                for idx, e in enumerate(events):
                    if e.get("k") == key:
                        if val_type is not None and e.get("t") != val_type:
                            continue
                        if content is not None and e.get("c") != content:
                            continue
                        return idx
                return -1

            def find_all_events(key, val_type=None):
                """Find all events of a given type."""
                result = []
                for e in events:
                    if e.get("k") == key:
                        if val_type is not None and e.get("t") != val_type:
                            continue
                        result.append(e)
                return result

            start_idx = find_event_index("markdown")
            self.assertNotEqual(
                start_idx, -1, f"Missing initial markdown event. Events: {events}"
            )
            self.assertIn(
                "Start text.",
                events[start_idx].get("c", ""),
                f"Initial markdown missing 'Start text.'. Got: {events[start_idx]}",
            )

            load_idx_code = find_event_index("load", val_type="code")
            self.assertNotEqual(
                load_idx_code,
                -1,
                f"Missing 'load' event with type='code'. Events: {events}",
            )
            self.assertEqual(events[load_idx_code].get("t"), "code")

            expected_code = "print('hello\\'world')\nprint(\"quoted\")\n"
            code_idx = find_event_index(
                "code", val_type="python", content=expected_code
            )
            self.assertNotEqual(
                code_idx,
                -1,
                f"Missing or incorrect Python code block. Expected: {repr(expected_code)}, "
                f"Events: {events}",
            )
            self.assertGreater(
                code_idx, load_idx_code, "Code event should appear after load event"
            )

            middle_events = find_all_events("markdown")
            has_middle = any(
                "Middle text with spacing." in e.get("c", "") for e in middle_events
            )
            self.assertTrue(
                has_middle,
                f"Missing middle markdown. All markdown events: {middle_events}",
            )

            load_idx_mermaid = find_event_index("load", val_type="mermaid")
            self.assertNotEqual(
                load_idx_mermaid,
                -1,
                f"Missing 'load' event with type='mermaid'. Events: {events}",
            )
            self.assertEqual(events[load_idx_mermaid].get("t"), "mermaid")

            expected_mermaid = "graph TD\nA-->B\nC--msg-->D\n"
            mermaid_idx = find_event_index("mermaid", content=expected_mermaid)
            self.assertNotEqual(
                mermaid_idx,
                -1,
                f"Missing or incorrect Mermaid block. Expected: {repr(expected_mermaid)}, "
                f"Events: {events}",
            )
            self.assertGreater(
                mermaid_idx,
                load_idx_mermaid,
                "Mermaid event should appear after load event",
            )

            empty_code_idx = find_event_index("code", val_type="javascript", content="")
            self.assertNotEqual(
                empty_code_idx,
                -1,
                f"Missing empty JavaScript code block. Events: {events}",
            )

            bash_expected = "echo \"Hello 世界\"\necho 'test\\\\path'\n"
            bash_idx = find_event_index("code", val_type="bash", content=bash_expected)
            self.assertNotEqual(
                bash_idx,
                -1,
                f"Missing or incorrect Bash code block with unicode/escapes. "
                f"Expected: {repr(bash_expected)}, Events: {events}",
            )

            final_events = find_all_events("markdown")
            has_final = any(
                "End text with special: @#$%." in e.get("c", "") for e in final_events
            )
            self.assertTrue(
                has_final,
                f"Missing final markdown. All markdown events: {final_events}",
            )

            self.assertLess(
                start_idx, code_idx, "Start markdown should appear before Python code"
            )
            self.assertGreater(
                mermaid_idx, code_idx, "Mermaid should appear after initial code"
            )
            last_markdown_idx = max(
                idx for idx, e in enumerate(events) if e.get("k") == "markdown"
            )
            self.assertGreater(
                last_markdown_idx,
                bash_idx,
                "Final markdown should appear after all code blocks",
            )

            # No unexpected event keys
            valid_keys = {"k", "c", "t", "m", "e", "h", "r", "n"}
            for i, event in enumerate(events):
                unexpected_keys = set(event.keys()) - valid_keys
                self.assertEqual(
                    unexpected_keys,
                    set(),
                    f"Event {i} has unexpected keys: {unexpected_keys}. Event: {event}",
                )

    def test_streamer_handles_tool_call(self):
        """Test that tool results are parsed using StreamParser."""
        # LLM responds with a tool call JSON
        fake_stream = [
            "data: " + json.dumps({"content": '{"tool": "show_user_resources"'}),
            "data: " + json.dumps({"content": ', "arguments": {}}'}),
        ]

        mock_response = Mock()
        mock_response.iter_lines.return_value = fake_stream
        mock_response.raise_for_status = Mock()

        # Mock user for tool execution
        mock_user = Mock()
        mock_user.id = 1
        mock_user.username = "testuser"

        # Mock tool executor to return a result with UI component info
        tool_result = {
            "type": "success",
            "data": {"resources": [], "total": 0, "type_counts": {}},
            "summary": "Found 0 resources",
            "ui_component": "table",
            "ui_data": {
                "h": ["Name", "Type", "State", "Project", "Customer"],
                "r": [],
                "n": 0,
            },
        }

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            with patch(
                "waldur_mastermind.chat.views.ToolExecutor.execute_tool"
            ) as mock_execute:
                mock_post.return_value.__enter__.return_value = mock_response
                mock_execute.return_value = tool_result

                streamer = LLMStreamer(
                    "show my resources",
                    "https://example.com/stream",
                    "dummy-token",
                    mock_user,
                )
                chunks = list(streamer)

                # Verify tool result was rendered as table via StreamParser
                found_tool_result = False
                for chunk in chunks:
                    data = json.loads(chunk)
                    if data.get("k") == "table" and data.get("n") == 0:
                        found_tool_result = True

                self.assertTrue(
                    found_tool_result,
                    f"Did not find tool result rendered as table. Chunks: {chunks}",
                )
                mock_execute.assert_called_once_with("show_user_resources", {})

    def test_streamer_hides_tool_errors(self):
        """Test that tool errors are not displayed to users."""
        fake_stream = [
            "data: " + json.dumps({"content": '{"tool": "unknown_tool"'}),
            "data: " + json.dumps({"content": ', "arguments": {}}'}),
        ]

        mock_response = Mock()
        mock_response.iter_lines.return_value = fake_stream
        mock_response.raise_for_status = Mock()

        mock_user = Mock()
        mock_user.id = 1

        # Mock tool executor to return an error
        tool_result = {
            "type": "error",
            "error": "Unknown tool: unknown_tool",
            "summary": "Unknown tool: unknown_tool",
        }

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            with patch(
                "waldur_mastermind.chat.views.ToolExecutor.execute_tool"
            ) as mock_execute:
                mock_post.return_value.__enter__.return_value = mock_response
                mock_execute.return_value = tool_result

                streamer = LLMStreamer(
                    "test", "https://example.com/stream", "dummy-token", mock_user
                )
                chunks = list(streamer)

                # Verify NO error message is displayed to user
                for chunk in chunks:
                    data = json.loads(chunk)
                    # Should not contain error message
                    if "c" in data:
                        self.assertNotIn(
                            "Unknown tool",
                            data["c"],
                            f"Error message leaked to user: {data}",
                        )

                mock_execute.assert_called_once()


class LLMStreamerUsageRecordingTest(drf_test.APITestCase):
    """Test that LLMStreamer persists token usage to TokenQuota after streaming."""

    def _fake_response(self, lines):
        resp = Mock()
        resp.iter_lines.return_value = lines
        resp.raise_for_status = Mock()
        return resp

    def _consume(self, streamer):
        return list(streamer)

    def test_records_usage_after_successful_stream(self):
        """Token counts from upstream usage_metadata are persisted to TokenQuota."""
        user = structure_factories.UserFactory()
        resp = self._fake_response(
            [
                "data: " + json.dumps({"content": "Hello"}),
                "data: "
                + json.dumps(
                    {
                        "additional_kwargs": {
                            "usage_metadata": {
                                "input_tokens": 200,
                                "output_tokens": 100,
                            }
                        }
                    }
                ),
            ]
        )

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = resp
            self._consume(LLMStreamer("hi", "https://llm/stream", "tok", user))

        quota = TokenQuota.objects.get(user=user)
        self.assertEqual(quota.daily_usage, 300)
        self.assertEqual(quota.weekly_usage, 300)
        self.assertEqual(quota.monthly_usage, 300)

    def test_skips_recording_when_zero_tokens_and_no_error(self):
        """No TokenQuota created when upstream reports 0 tokens and no error."""
        user = structure_factories.UserFactory()
        resp = self._fake_response(
            [
                "data: " + json.dumps({"content": "Hi"}),
                "data: "
                + json.dumps(
                    {
                        "additional_kwargs": {
                            "usage_metadata": {"input_tokens": 0, "output_tokens": 0}
                        }
                    }
                ),
            ]
        )

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = resp
            self._consume(LLMStreamer("hi", "https://llm/stream", "tok", user))

        self.assertFalse(TokenQuota.objects.filter(user=user).exists())

    def test_no_user_skips_recording_entirely(self):
        """Streamer with user=None does not create or touch any TokenQuota."""
        resp = self._fake_response(
            [
                "data: " + json.dumps({"content": "Hello"}),
                "data: "
                + json.dumps(
                    {
                        "additional_kwargs": {
                            "usage_metadata": {
                                "input_tokens": 500,
                                "output_tokens": 200,
                            }
                        }
                    }
                ),
            ]
        )

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = resp
            self._consume(LLMStreamer("hi", "https://llm/stream", "tok", user=None))

        self.assertEqual(TokenQuota.objects.count(), 0)

    def test_accumulates_usage_across_sequential_streams(self):
        """Multiple streams for the same user accumulate correctly."""
        user = structure_factories.UserFactory()
        token_pairs = [(100, 50), (200, 100), (50, 25)]  # total = 525

        for inp, out in token_pairs:
            resp = self._fake_response(
                [
                    "data: " + json.dumps({"content": "Hi"}),
                    "data: "
                    + json.dumps(
                        {
                            "additional_kwargs": {
                                "usage_metadata": {
                                    "input_tokens": inp,
                                    "output_tokens": out,
                                }
                            }
                        }
                    ),
                ]
            )
            with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
                mock_post.return_value.__enter__.return_value = resp
                self._consume(LLMStreamer("hi", "https://llm/stream", "tok", user))

        quota = TokenQuota.objects.get(user=user)
        self.assertEqual(quota.daily_usage, 525)
        self.assertEqual(quota.weekly_usage, 525)
        self.assertEqual(quota.monthly_usage, 525)

    def test_records_partial_tokens_before_stream_error(self):
        """Tokens received before a connection error are still persisted."""
        user = structure_factories.UserFactory()

        def failing_lines():
            yield "data: " + json.dumps(
                {
                    "content": "partial",
                    "additional_kwargs": {
                        "usage_metadata": {"input_tokens": 150, "output_tokens": 75}
                    },
                }
            )
            raise requests.ConnectionError("Connection dropped")

        resp = Mock()
        resp.iter_lines.return_value = failing_lines()
        resp.raise_for_status = Mock()

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = resp
            self._consume(LLMStreamer("hi", "https://llm/stream", "tok", user))

        quota = TokenQuota.objects.get(user=user)
        self.assertEqual(quota.daily_usage, 225)
        self.assertEqual(quota.weekly_usage, 225)
        self.assertEqual(quota.monthly_usage, 225)

    def test_records_zero_usage_on_error_with_no_tokens(self):
        """On upstream error with 0 tokens, quota is still created (error path taken)."""
        user = structure_factories.UserFactory()

        resp = Mock()
        resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        resp.iter_lines.return_value = []

        with patch("waldur_mastermind.chat.views.requests.post") as mock_post:
            mock_post.return_value.__enter__.return_value = resp
            self._consume(LLMStreamer("hi", "https://llm/stream", "tok", user))

        quota = TokenQuota.objects.get(user=user)
        self.assertEqual(quota.daily_usage, 0)
        self.assertEqual(quota.weekly_usage, 0)
        self.assertEqual(quota.monthly_usage, 0)
