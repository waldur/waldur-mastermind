import json
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import openai
from rest_framework import test as drf_test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.llm_streamer import LLMStreamer
from waldur_mastermind.chat.models import TokenQuota
from waldur_mastermind.chat.tests.utils import (
    _make_chunk,
    _make_tool_call_delta,
    _mock_openai_client,
)
from waldur_mastermind.chat.ui_registry import ui_registry  # noqa: F401

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


def _messages(text="hi"):
    return [{"role": "user", "content": text}]


class LLMStreamerTest(unittest.TestCase):
    def setUp(self):
        # Patch constance config to avoid DB access and patch the OpenAI client
        config_patcher = patch(
            "waldur_mastermind.chat.llm_streamer.config",
            LLM_INFERENCES_MODEL="test-model",
            LLM_INFERENCES_API_URL="https://example.com/v1",
            LLM_INFERENCES_API_TOKEN="tok",
            LLM_CHAT_ENABLED=True,
            LLM_CHAT_ENABLED_ROLES="all",
            LLM_INFERENCES_BACKEND_TYPE="generic",
            LLM_COMPLETION_KWARGS={},
        )
        self.mock_config = config_patcher.start()
        self.addCleanup(config_patcher.stop)

    def _make_streamer(self, chunks, messages=None, user=None):
        streamer = LLMStreamer(
            messages or _messages(),
            "https://example.com/v1",
            "dummy-token",
            user=user,
        )
        streamer.client = _mock_openai_client(chunks)
        return streamer

    def test_streamer_parses_content(self):
        chunks = [_make_chunk(content="Hello")]
        streamer = self._make_streamer(chunks)
        output = list(streamer)

        found_hello = any(
            json.loads(c).get("k") == "markdown" and json.loads(c).get("c") == "Hello"
            for c in output
        )
        self.assertTrue(found_hello, f"Did not find 'Hello' UI event in: {output}")

    def test_streamer_parses_code_block(self):
        chunks = [
            _make_chunk(content="```python\n"),
            _make_chunk(content="def hello():\n"),
            _make_chunk(content="    print('world')\n"),
            _make_chunk(content="```"),
        ]
        streamer = self._make_streamer(chunks)
        output = [json.loads(c) for c in streamer]

        self.assertTrue(
            any(e.get("k") == "load" and e.get("t") == "code" for e in output),
            f"No load event. Output: {output}",
        )
        expected = "def hello():\n    print('world')\n"
        self.assertTrue(
            any(
                e.get("k") == "code"
                and e.get("c") == expected
                and e.get("t") == "python"
                for e in output
            ),
            f"No python code event. Output: {output}",
        )

    def test_streamer_parses_split_delimiter(self):
        chunks = [
            _make_chunk(content="Here is code ``"),
            _make_chunk(content="`python\nprint('split')\n```"),
        ]
        streamer = self._make_streamer(chunks)
        output = [json.loads(c) for c in streamer]

        self.assertTrue(
            any(
                e.get("k") == "code"
                and e.get("c") == "print('split')\n"
                and e.get("t") == "python"
                for e in output
            ),
            "Did not find Code UI event with split delimiter",
        )

    def test_streamer_yields_loading_status(self):
        chunks = [_make_chunk(content="Here is some code:\n```python\n")]
        streamer = self._make_streamer(chunks)
        output = [json.loads(c) for c in streamer]

        self.assertTrue(
            any(e.get("k") == "load" and e.get("t") == "code" for e in output),
            "Did not find Loading UI event",
        )

    def test_streamer_parses_mermaid_block(self):
        chunks = [
            _make_chunk(content="Here is a diagram:\n```mermaid\ngraph TD\nA-->B\n```")
        ]
        streamer = self._make_streamer(chunks)
        output = [json.loads(c) for c in streamer]

        self.assertTrue(
            any(
                e.get("k") == "mermaid" and e.get("c") == "graph TD\nA-->B\n"
                for e in output
            ),
            "Did not find Mermaid UI event",
        )

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
        chunks = [
            _make_chunk(content="Start text. "),
            _make_chunk(content="Here is code: ```py"),
            _make_chunk(content="thon\nprint('hello\\'world')\nprint(\"quoted\")\n"),
            _make_chunk(content="```\n   Middle text with spacing.   "),
            _make_chunk(content="Diagram: ```mer"),
            _make_chunk(content="maid\ngraph TD\nA-->B\nC--msg-->D\n"),
            _make_chunk(content="```\nEmpty code: ```javascript"),
            _make_chunk(content="\n```\n"),
            _make_chunk(
                content="Final: ```bash\necho \"Hello 世界\"\necho 'test\\\\path'\n"
            ),
            _make_chunk(content="```\nEnd text with special: @#$%."),
        ]
        streamer = self._make_streamer(chunks)
        events = [json.loads(c) for c in streamer]

        def find_event_index(key, val_type=None, content=None):
            for idx, e in enumerate(events):
                if e.get("k") == key:
                    if val_type is not None and e.get("t") != val_type:
                        continue
                    if content is not None and e.get("c") != content:
                        continue
                    return idx
            return -1

        def find_all_events(key):
            return [e for e in events if e.get("k") == key]

        start_idx = find_event_index("markdown")
        self.assertNotEqual(start_idx, -1, "Missing initial markdown event")
        self.assertIn("Start text.", events[start_idx].get("c", ""))

        load_idx_code = find_event_index("load", val_type="code")
        self.assertNotEqual(load_idx_code, -1, "Missing load event for code")

        expected_code = "print('hello\\'world')\nprint(\"quoted\")\n"
        code_idx = find_event_index("code", val_type="python", content=expected_code)
        self.assertNotEqual(
            code_idx, -1, f"Missing python code block. Events: {events}"
        )
        self.assertGreater(code_idx, load_idx_code)

        has_middle = any(
            "Middle text with spacing." in e.get("c", "")
            for e in find_all_events("markdown")
        )
        self.assertTrue(has_middle, "Missing middle markdown")

        load_idx_mermaid = find_event_index("load", val_type="mermaid")
        self.assertNotEqual(load_idx_mermaid, -1, "Missing mermaid load event")

        expected_mermaid = "graph TD\nA-->B\nC--msg-->D\n"
        mermaid_idx = find_event_index("mermaid", content=expected_mermaid)
        self.assertNotEqual(mermaid_idx, -1, "Missing mermaid block")
        self.assertGreater(mermaid_idx, load_idx_mermaid)

        empty_code_idx = find_event_index("code", val_type="javascript", content="")
        self.assertNotEqual(empty_code_idx, -1, "Missing empty javascript code block")

        bash_expected = "echo \"Hello 世界\"\necho 'test\\\\path'\n"
        bash_idx = find_event_index("code", val_type="bash", content=bash_expected)
        self.assertNotEqual(bash_idx, -1, "Missing bash code block")

        has_final = any(
            "End text with special: @#$%." in e.get("c", "")
            for e in find_all_events("markdown")
        )
        self.assertTrue(has_final, "Missing final markdown")

        self.assertLess(start_idx, code_idx)
        self.assertGreater(mermaid_idx, code_idx)

        valid_keys = {"k", "c", "t", "m", "e", "h", "r", "n"}
        for i, event in enumerate(events):
            unexpected = set(event.keys()) - valid_keys
            self.assertEqual(
                unexpected, set(), f"Event {i} has unexpected keys: {event}"
            )

    def test_streamer_handles_tool_call(self):
        """Test that OpenAI tool_calls are detected and executed server-side."""
        tc1 = _make_tool_call_delta(0, name="show_user_resources", call_id="call_abc")
        tc2 = _make_tool_call_delta(0, arguments="{}")
        chunks = [
            _make_chunk(tool_calls=[tc1]),
            _make_chunk(tool_calls=[tc2]),
        ]

        tool_result = {
            "type": "success",
            "summary": "Found 0 resources",
            "ui_component": "table",
            "ui_data": {
                "h": ["Name", "Type", "State", "Project", "Customer"],
                "r": [],
                "n": 0,
            },
        }

        mock_user = Mock()
        mock_user.id = 1
        mock_user.username = "testuser"

        with patch(
            "waldur_mastermind.chat.llm_streamer.ToolExecutor.execute_tool"
        ) as mock_exec:
            mock_exec.return_value = tool_result
            streamer = self._make_streamer(chunks, user=mock_user)
            output = [json.loads(c) for c in streamer]

        self.assertTrue(
            any(e.get("k") == "table" and e.get("n") == 0 for e in output),
            f"Did not find tool result rendered as table. Output: {output}",
        )
        mock_exec.assert_called_once_with("show_user_resources", {})

    def test_streamer_hides_tool_errors(self):
        """Test that tool errors are not displayed to users."""
        tc = _make_tool_call_delta(
            0, name="unknown_tool", arguments="{}", call_id="call_x"
        )
        chunks = [_make_chunk(tool_calls=[tc])]

        tool_result = {
            "type": "error",
            "summary": "Unknown tool: unknown_tool",
        }

        mock_user = Mock()
        mock_user.id = 1

        with patch(
            "waldur_mastermind.chat.llm_streamer.ToolExecutor.execute_tool"
        ) as mock_exec:
            mock_exec.return_value = tool_result
            streamer = self._make_streamer(chunks, user=mock_user)
            output = [json.loads(c) for c in streamer]

        for event in output:
            if "c" in event:
                self.assertNotIn("Unknown tool", event["c"])

        mock_exec.assert_called_once()


class LLMStreamerUsageRecordingTest(drf_test.APITestCase):
    """Test that LLMStreamer persists token usage to TokenQuota after streaming."""

    def setUp(self):
        config_patcher = patch(
            "waldur_mastermind.chat.llm_streamer.config",
            LLM_INFERENCES_MODEL="test-model",
            LLM_INFERENCES_API_URL="https://example.com/v1",
            LLM_INFERENCES_API_TOKEN="tok",
            LLM_CHAT_ENABLED=True,
            LLM_CHAT_ENABLED_ROLES="all",
            LLM_INFERENCES_BACKEND_TYPE="generic",
            LLM_COMPLETION_KWARGS={},
        )
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

    def _make_streamer(self, chunks, user=None):
        streamer = LLMStreamer(_messages(), "https://example.com/v1", "tok", user=user)
        streamer.client = _mock_openai_client(chunks)
        return streamer

    def test_records_usage_after_successful_stream(self):
        """Token counts from OpenAI usage chunk are persisted to TokenQuota."""
        user = structure_factories.UserFactory()
        chunks = [
            _make_chunk(content="Hello"),
            _make_chunk(usage={"prompt_tokens": 200, "completion_tokens": 100}),
        ]
        list(self._make_streamer(chunks, user=user))

        quota = TokenQuota.objects.get(user=user)
        self.assertEqual(quota.daily_usage, 300)
        self.assertEqual(quota.weekly_usage, 300)
        self.assertEqual(quota.monthly_usage, 300)

    def test_skips_recording_when_zero_tokens_and_no_error(self):
        """No TokenQuota created when upstream reports 0 tokens and no error."""
        user = structure_factories.UserFactory()
        chunks = [
            _make_chunk(content="Hi"),
            _make_chunk(usage={"prompt_tokens": 0, "completion_tokens": 0}),
        ]
        list(self._make_streamer(chunks, user=user))

        self.assertFalse(TokenQuota.objects.filter(user=user).exists())

    def test_no_user_skips_recording_entirely(self):
        """Streamer with user=None does not create or touch any TokenQuota."""
        chunks = [
            _make_chunk(content="Hello"),
            _make_chunk(usage={"prompt_tokens": 500, "completion_tokens": 200}),
        ]
        list(self._make_streamer(chunks, user=None))

        self.assertEqual(TokenQuota.objects.count(), 0)

    def test_accumulates_usage_across_sequential_streams(self):
        """Multiple streams for the same user accumulate correctly."""
        user = structure_factories.UserFactory()
        token_pairs = [(100, 50), (200, 100), (50, 25)]  # total = 525

        for inp, out in token_pairs:
            chunks = [
                _make_chunk(content="Hi"),
                _make_chunk(usage={"prompt_tokens": inp, "completion_tokens": out}),
            ]
            list(self._make_streamer(chunks, user=user))

        quota = TokenQuota.objects.get(user=user)
        self.assertEqual(quota.daily_usage, 525)
        self.assertEqual(quota.weekly_usage, 525)
        self.assertEqual(quota.monthly_usage, 525)

    def test_records_partial_tokens_before_stream_error(self):
        """Tokens received before a connection error are still persisted."""
        user = structure_factories.UserFactory()

        def failing_chunks():
            yield _make_chunk(content="partial")
            yield _make_chunk(usage={"prompt_tokens": 150, "completion_tokens": 75})
            raise openai.APIConnectionError(request=MagicMock())

        streamer = LLMStreamer(_messages(), "https://example.com/v1", "tok", user=user)

        @contextmanager
        def _fail_stream(chunks):
            yield failing_chunks()

        streamer.client = MagicMock()
        streamer.client.chat.completions.create.return_value = _fail_stream(None)
        list(streamer)

        quota = TokenQuota.objects.get(user=user)
        self.assertEqual(quota.daily_usage, 225)
        self.assertEqual(quota.weekly_usage, 225)
        self.assertEqual(quota.monthly_usage, 225)

    def test_records_zero_usage_on_error_with_no_tokens(self):
        """On upstream error with 0 tokens, quota is still created (error path taken)."""
        user = structure_factories.UserFactory()

        streamer = LLMStreamer(_messages(), "https://example.com/v1", "tok", user=user)

        @contextmanager
        def _error_stream(chunks):
            raise openai.APIStatusError("500", response=MagicMock(), body=None)
            yield  # make it a generator

        streamer.client = MagicMock()
        streamer.client.chat.completions.create.return_value = _error_stream(None)
        list(streamer)

        quota = TokenQuota.objects.get(user=user)
        self.assertEqual(quota.daily_usage, 0)
        self.assertEqual(quota.weekly_usage, 0)
        self.assertEqual(quota.monthly_usage, 0)


class LLMStreamerCompletionKwargsTest(unittest.TestCase):
    """Test the layered kwargs system in _stream_completion."""

    def _make_streamer_with_config(self, mock_config):
        """Construct a streamer inside an active config patch so self.model is set correctly."""
        streamer = LLMStreamer(
            [{"role": "user", "content": "hi"}],
            "https://example.com/v1",
            "dummy-token",
            user=None,
        )
        streamer.client = MagicMock()
        streamer.client.chat.completions.create.return_value = MagicMock()
        return streamer

    def _call_and_get_kwargs(self, streamer):
        """Call _stream_completion and return the kwargs passed to the client."""
        streamer._stream_completion(streamer.messages)
        _, call_kwargs = streamer.client.chat.completions.create.call_args
        return call_kwargs

    def test_default_provider_kwargs_ollama(self):
        """backend=ollama, LLM_COMPLETION_KWARGS={} => temperature=0.7, top_p=0.8."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            LLM_INFERENCES_MODEL="test-model",
            LLM_INFERENCES_BACKEND_TYPE="ollama",
            LLM_COMPLETION_KWARGS={},
        ) as mock_config:
            streamer = self._make_streamer_with_config(mock_config)
            kwargs = self._call_and_get_kwargs(streamer)

        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["top_p"], 0.8)
        self.assertNotIn("presence_penalty", kwargs)
        self.assertNotIn("extra_body", kwargs)

    def test_default_provider_kwargs_vllm(self):
        """backend=vllm, LLM_COMPLETION_KWARGS={} => includes presence_penalty and extra_body."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            LLM_INFERENCES_MODEL="test-model",
            LLM_INFERENCES_BACKEND_TYPE="vllm",
            LLM_COMPLETION_KWARGS={},
        ) as mock_config:
            streamer = self._make_streamer_with_config(mock_config)
            kwargs = self._call_and_get_kwargs(streamer)

        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["top_p"], 0.8)
        self.assertEqual(kwargs["presence_penalty"], 1.5)
        self.assertIn("extra_body", kwargs)
        self.assertEqual(kwargs["extra_body"]["top_k"], 20)
        self.assertFalse(
            kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"]
        )

    def test_override_merges_with_provider(self):
        """backend=vllm, LLM_COMPLETION_KWARGS={"temperature": 0.5} => temperature=0.5 + rest of vllm defaults."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            LLM_INFERENCES_MODEL="test-model",
            LLM_INFERENCES_BACKEND_TYPE="vllm",
            LLM_COMPLETION_KWARGS={"temperature": 0.5},
        ) as mock_config:
            streamer = self._make_streamer_with_config(mock_config)
            kwargs = self._call_and_get_kwargs(streamer)

        self.assertEqual(kwargs["temperature"], 0.5)
        self.assertEqual(kwargs["top_p"], 0.8)
        self.assertEqual(kwargs["presence_penalty"], 1.5)

    def test_unknown_provider_uses_fallback(self):
        """backend='custom_provider', LLM_COMPLETION_KWARGS={} => FALLBACK_DEFAULTS applied."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            LLM_INFERENCES_MODEL="test-model",
            LLM_INFERENCES_BACKEND_TYPE="custom_provider",
            LLM_COMPLETION_KWARGS={},
        ) as mock_config:
            streamer = self._make_streamer_with_config(mock_config)
            kwargs = self._call_and_get_kwargs(streamer)

        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["top_p"], 0.8)
        self.assertNotIn("presence_penalty", kwargs)

    def test_protected_keys_ignored(self):
        """LLM_COMPLETION_KWARGS with protected keys are ignored and a warning is logged."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            LLM_INFERENCES_MODEL="real-model",
            LLM_INFERENCES_BACKEND_TYPE="ollama",
            LLM_COMPLETION_KWARGS={"model": "evil-model", "temperature": 0.5},
        ) as mock_config:
            streamer = self._make_streamer_with_config(mock_config)
            with patch("waldur_mastermind.chat.llm_streamer.logger") as mock_logger:
                kwargs = self._call_and_get_kwargs(streamer)

        self.assertEqual(kwargs["model"], "real-model")
        self.assertEqual(kwargs["temperature"], 0.5)
        mock_logger.warning.assert_called_once()
        warning_call = mock_logger.warning.call_args[0]
        self.assertIn("protected keys", warning_call[0])

    def test_empty_override(self):
        """LLM_COMPLETION_KWARGS={} => pure provider defaults, no extra keys."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            LLM_INFERENCES_MODEL="test-model",
            LLM_INFERENCES_BACKEND_TYPE="openai",
            LLM_COMPLETION_KWARGS={},
        ) as mock_config:
            streamer = self._make_streamer_with_config(mock_config)
            kwargs = self._call_and_get_kwargs(streamer)

        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["top_p"], 0.8)
        self.assertNotIn("presence_penalty", kwargs)
        self.assertNotIn("extra_body", kwargs)
