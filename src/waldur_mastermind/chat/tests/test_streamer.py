import json
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import openai
from django.utils import timezone
from rest_framework import test as drf_test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.llm_streamer import _CANCEL_CHECK_INTERVAL, LLMStreamer
from waldur_mastermind.chat.models import (
    ChatSession,
    Message,
    ThreadSession,
    TokenQuota,
)
from waldur_mastermind.chat.tests.utils import (
    SYNC_THREAD_PATCH,
    _make_chunk,
    _make_tool_call_delta,
    _mock_openai_client,
    _SynchronousThread,
)
from waldur_mastermind.chat.ui_registry import ui_registry  # noqa: F401

"""
NDJSON streaming response format for chat messages.

Uses single-character keys for bandwidth optimization. Each line is a JSON object
containing one or more of these fields:

- k: Component key (markdown, code, mermaid, load, vm_order, resource_list)
- c: Content payload (text)
- t: Type/tag (language for code blocks, component for loading)
- m: Metadata (object with additional info like token counts)
- e: Error message (string)

Examples:
    {"k":"markdown","c":"Hello!"}
    {"k":"code","c":"print('hi')","t":"python"}
    {"k":"resource_list","project_uuid":"abc..."}
    {"m":{"tokens":150}}
    {"e":"Request failed"}
"""


def _messages(text="hi"):
    return [{"role": "user", "content": text}]


class _LLMStreamerTestBase:
    """Shared setUp and helpers for LLM streamer tests."""

    def setUp(self):
        super().setUp()
        config_patcher = patch(
            "waldur_mastermind.chat.llm_streamer.config",
            AI_ASSISTANT_MODEL="test-model",
            AI_ASSISTANT_API_URL="https://example.com/v1",
            AI_ASSISTANT_API_TOKEN="tok",
            AI_ASSISTANT_ENABLED=True,
            AI_ASSISTANT_ENABLED_ROLES="all",
            AI_ASSISTANT_BACKEND_TYPE="generic",
            AI_ASSISTANT_COMPLETION_KWARGS={},
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


class LLMStreamerTest(_LLMStreamerTestBase, unittest.TestCase):
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
            "summary": "Showing resources",
            "ui_component": "resource_list",
            "ui_data": {},
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

        # Loading indicator emitted before tool execution
        self.assertTrue(
            any(e.get("k") == "load" and e.get("t") == "tool" for e in output),
            f"Did not find tool loading indicator. Output: {output}",
        )

        # Tool result rendered as resource_list
        self.assertTrue(
            any(e.get("k") == "resource_list" for e in output),
            f"Did not find tool result rendered as resource_list. Output: {output}",
        )

        # Loading indicator appears before tool result
        load_idx = next(
            i
            for i, e in enumerate(output)
            if e.get("k") == "load" and e.get("t") == "tool"
        )
        resource_list_idx = next(
            i for i, e in enumerate(output) if e.get("k") == "resource_list"
        )
        self.assertLess(load_idx, resource_list_idx)

        mock_exec.assert_called_once_with("show_user_resources", {})

    def test_streamer_stores_tool_result_for_persistence(self):
        """Test that tool results are stored in tool_calls for DB persistence."""
        tc = _make_tool_call_delta(
            0, name="show_user_resources", arguments="{}", call_id="call_abc"
        )
        chunks = [_make_chunk(tool_calls=[tc])]

        tool_result = {
            "type": "success",
            "summary": "Showing resources",
            "ui_component": "resource_list",
            "ui_data": {"project_uuid": "abc123"},
        }

        mock_user = Mock()
        mock_user.id = 1
        mock_user.username = "testuser"

        with patch(
            "waldur_mastermind.chat.llm_streamer.ToolExecutor.execute_tool"
        ) as mock_exec:
            mock_exec.return_value = tool_result
            streamer = self._make_streamer(chunks, user=mock_user)
            list(streamer)  # Consume the stream

        # Verify serialized tool calls include the result
        serialized = streamer._serialized_tool_calls()
        self.assertEqual(len(serialized), 1)
        self.assertIn("result", serialized[0])
        self.assertEqual(serialized[0]["result"]["k"], "resource_list")
        self.assertEqual(serialized[0]["result"]["project_uuid"], "abc123")

    def test_streamer_hides_tool_errors(self):
        """Test that tool errors are not displayed to users and not persisted."""
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

        # Error results should NOT be stored for persistence
        serialized = streamer._serialized_tool_calls()
        self.assertEqual(len(serialized), 1)
        self.assertNotIn("result", serialized[0])


class LLMStreamerDisconnectTest(_LLMStreamerTestBase, unittest.TestCase):
    """Test that client disconnects freeze content accumulation.

    When the client disconnects (or cancels), the worker keeps draining the
    LLM stream so it can capture the final usage-only chunk (accurate token
    counts), but content and tool-call accumulation is skipped.
    """

    def test_disconnect_accumulates_full_content(self):
        """When client disconnects mid-stream, worker keeps accumulating all content."""
        gate = threading.Event()
        # First chunk must be >= 50 chars so the parser flushes to the queue
        # and next(gen) on the main thread can return.
        first_chunk = "A" * 60

        def slow_chunks():
            yield _make_chunk(content=first_chunk)
            gate.wait(timeout=10)
            yield _make_chunk(content="beautiful ")
            yield _make_chunk(content="world")

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
        )
        mock_client = MagicMock()
        mock_stream = MagicMock()
        mock_stream.__enter__ = Mock(return_value=slow_chunks())
        mock_stream.__exit__ = Mock(return_value=False)
        mock_client.chat.completions.create.return_value = mock_stream
        streamer.client = mock_client

        gen = iter(streamer)
        next(gen)
        gen.close()

        gate.set()

        self.assertIsNotNone(streamer._worker_thread)
        streamer._worker_thread.join(timeout=5)
        self.assertFalse(streamer._worker_thread.is_alive())

        # All content accumulated — disconnect doesn't freeze content
        self.assertEqual(
            streamer.accumulated_content, first_chunk + "beautiful " + "world"
        )

    def test_disconnect_does_not_block_worker(self):
        """Worker thread completes promptly after client disconnects, even with many chunks."""
        chunks = [_make_chunk(content=f"chunk{i} ") for i in range(500)]
        streamer = self._make_streamer(chunks)
        gen = iter(streamer)

        next(gen)
        gen.close()

        # Worker should finish in reasonable time (not stuck on full queue)
        streamer._worker_thread.join(timeout=10)
        self.assertFalse(
            streamer._worker_thread.is_alive(),
            "Worker thread is still alive after timeout — likely blocked on full queue",
        )

    def test_disconnect_still_records_usage(self):
        """Usage chunk is still processed even after client disconnect."""
        gate = threading.Event()
        # First chunk must be >= 50 chars so the parser flushes to the queue
        first_chunk = "B" * 60

        def chunks_with_usage():
            yield _make_chunk(content=first_chunk)
            gate.wait(timeout=10)
            yield _make_chunk(content="world")
            yield _make_chunk(usage={"prompt_tokens": 42, "completion_tokens": 18})

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
        )
        mock_client = MagicMock()
        mock_stream = MagicMock()
        mock_stream.__enter__ = Mock(return_value=chunks_with_usage())
        mock_stream.__exit__ = Mock(return_value=False)
        mock_client.chat.completions.create.return_value = mock_stream
        streamer.client = mock_client

        gen = iter(streamer)
        next(gen)
        gen.close()

        gate.set()

        streamer._worker_thread.join(timeout=5)

        # All content accumulated — disconnect doesn't freeze content
        self.assertEqual(streamer.accumulated_content, first_chunk + "world")
        # Usage chunk was still processed
        self.assertEqual(streamer.input_tokens, 42)
        self.assertEqual(streamer.output_tokens, 18)

    def test_disconnect_skips_tool_execution(self):
        """Tool calls are not executed when client has disconnected."""
        gate = threading.Event()
        tc1 = _make_tool_call_delta(0, name="show_user_resources", call_id="call_abc")
        tc2 = _make_tool_call_delta(0, arguments="{}")
        # First chunk must be >= 50 chars so the parser flushes to the queue
        first_chunk = "C" * 60

        def chunks_with_tool():
            yield _make_chunk(content=first_chunk)
            gate.wait(timeout=10)
            yield _make_chunk(tool_calls=[tc1])
            yield _make_chunk(tool_calls=[tc2])

        mock_user = Mock()
        mock_user.id = 1
        mock_user.username = "testuser"

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            user=mock_user,
        )
        mock_client = MagicMock()
        mock_stream = MagicMock()
        mock_stream.__enter__ = Mock(return_value=chunks_with_tool())
        mock_stream.__exit__ = Mock(return_value=False)
        mock_client.chat.completions.create.return_value = mock_stream
        streamer.client = mock_client

        with patch(
            "waldur_mastermind.chat.llm_streamer.ToolExecutor.execute_tool"
        ) as mock_exec:
            gen = iter(streamer)
            next(gen)
            gen.close()

            gate.set()
            streamer._worker_thread.join(timeout=5)

            mock_exec.assert_not_called()

    def test_canned_response_no_worker_thread(self):
        """Canned response path runs synchronously without spawning a worker thread."""
        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            canned_response="This input has been blocked.",
        )
        list(streamer)

        self.assertIsNone(streamer._worker_thread)
        self.assertEqual(streamer.accumulated_content, "This input has been blocked.")

    def test_normal_stream_completes_identically(self):
        """Normal (no disconnect) stream produces the same output as before."""
        chunks = [
            _make_chunk(content="Hello world"),
            _make_chunk(usage={"prompt_tokens": 10, "completion_tokens": 5}),
        ]
        streamer = self._make_streamer(chunks)
        output = list(streamer)

        found = any(
            json.loads(c).get("k") == "markdown"
            and json.loads(c).get("c") == "Hello world"
            for c in output
        )
        self.assertTrue(found, f"Did not find expected content in: {output}")
        self.assertEqual(streamer.accumulated_content, "Hello world")

    def test_client_gone_set_after_disconnect(self):
        """_client_gone event is set after client disconnects via GeneratorExit."""
        chunks = [_make_chunk(content="Hello")]
        streamer = self._make_streamer(chunks)
        gen = iter(streamer)

        next(gen)
        self.assertFalse(streamer._client_gone.is_set())
        gen.close()

        self.assertTrue(streamer._client_gone.is_set())

    @patch("waldur_mastermind.chat.llm_streamer._WORKER_TIMEOUT", 0)
    def test_timeout_path_persists_via_worker(self):
        """When the consumer times out, the worker still persists messages."""
        # Use an event to hold the worker back so the consumer hits
        # an Empty queue.get() and triggers the timeout check.
        gate = threading.Event()

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
        )

        persist_call_count = 0
        original_persist = streamer._persist_messages

        def counting_persist():
            nonlocal persist_call_count
            persist_call_count += 1
            original_persist()

        streamer._persist_messages = counting_persist

        def delayed_workflow():
            gate.wait(timeout=10)
            # Simulate a minimal LLM response
            streamer.accumulated_content = "delayed"

        streamer._run_llm_workflow = delayed_workflow

        # Consume: the consumer should time out while the worker is blocked
        output = list(streamer)

        # Release the worker so it can finish
        gate.set()

        if streamer._worker_thread:
            streamer._worker_thread.join(timeout=5)

        # The timeout path must set _client_gone so _enqueue discards
        self.assertTrue(streamer._client_gone.is_set())
        # Worker always persists exactly once
        self.assertEqual(persist_call_count, 1)
        # Verify timeout error event was sent to client
        parsed = [json.loads(line) for line in output]
        error_events = [e for e in parsed if "e" in e]
        self.assertTrue(
            any("timed out" in e.get("e", "").lower() for e in error_events),
            f"Expected timeout error event in output: {output}",
        )

    def test_error_during_disconnect_still_completes(self):
        """If LLM errors while client is disconnected, worker still finishes."""
        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
        )

        @contextmanager
        def _error_stream(_):
            yield iter([_make_chunk(content="partial")])
            raise openai.APIConnectionError(request=MagicMock())

        streamer.client = MagicMock()
        streamer.client.chat.completions.create.return_value = _error_stream(None)

        gen = iter(streamer)
        next(gen)
        gen.close()

        streamer._worker_thread.join(timeout=5)
        self.assertFalse(streamer._worker_thread.is_alive())
        self.assertEqual(streamer.accumulated_content, "partial")


@patch(SYNC_THREAD_PATCH, _SynchronousThread)
class LLMStreamerUsageRecordingTest(_LLMStreamerTestBase, drf_test.APITestCase):
    """Test that LLMStreamer persists token usage to TokenQuota after streaming."""

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


class LLMStreamerErrorMessagesTest(_LLMStreamerTestBase, unittest.TestCase):
    """Test that specific OpenAI errors yield distinct user-facing messages."""

    def _make_error_streamer(self, error):
        """Return a streamer whose stream raises the given error."""
        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "tok",
            user=None,
        )

        @contextmanager
        def _error_stream(_):
            raise error
            yield  # noqa: unreachable — makes it a generator

        streamer.client = MagicMock()
        streamer.client.chat.completions.create.return_value = _error_stream(None)
        return streamer

    def _get_error_message(self, streamer):
        """Consume the streamer and return the error message from the 'e' event."""
        events = [json.loads(line) for line in streamer]
        error_events = [e for e in events if "e" in e]
        self.assertEqual(len(error_events), 1, f"Expected 1 error event, got {events}")
        return error_events[0]["e"]

    def test_authentication_error(self):
        error = openai.AuthenticationError(
            "invalid api key",
            response=MagicMock(),
            body=None,
        )
        msg = self._get_error_message(self._make_error_streamer(error))
        self.assertIn("authentication", msg.lower())

    def test_rate_limit_error(self):
        error = openai.RateLimitError(
            "rate limit",
            response=MagicMock(),
            body=None,
        )
        msg = self._get_error_message(self._make_error_streamer(error))
        self.assertIn("rate limit", msg.lower())

    def test_connection_error(self):
        error = openai.APIConnectionError(request=MagicMock())
        msg = self._get_error_message(self._make_error_streamer(error))
        self.assertIn("connect", msg.lower())

    def test_timeout_error(self):
        error = openai.APITimeoutError(request=MagicMock())
        msg = self._get_error_message(self._make_error_streamer(error))
        self.assertIn("timed out", msg.lower())

    def test_internal_server_error(self):
        error = openai.InternalServerError(
            "internal error",
            response=MagicMock(),
            body=None,
        )
        msg = self._get_error_message(self._make_error_streamer(error))
        self.assertIn("unavailable", msg.lower())

    def test_generic_api_error_fallback(self):
        error = openai.APIStatusError(
            "bad request",
            response=MagicMock(),
            body=None,
        )
        msg = self._get_error_message(self._make_error_streamer(error))
        self.assertIn("error", msg.lower())

    def test_unexpected_exception_uses_default(self):
        error = RuntimeError("something broke")
        msg = self._get_error_message(self._make_error_streamer(error))
        self.assertIn("interrupted", msg.lower())


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
        """backend=ollama, AI_ASSISTANT_COMPLETION_KWARGS={} => temperature=0.7, top_p=0.8."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            AI_ASSISTANT_MODEL="test-model",
            AI_ASSISTANT_BACKEND_TYPE="ollama",
            AI_ASSISTANT_COMPLETION_KWARGS={},
        ) as mock_config:
            streamer = self._make_streamer_with_config(mock_config)
            kwargs = self._call_and_get_kwargs(streamer)

        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["top_p"], 0.8)
        self.assertNotIn("presence_penalty", kwargs)
        self.assertNotIn("extra_body", kwargs)

    def test_default_provider_kwargs_vllm(self):
        """backend=vllm, AI_ASSISTANT_COMPLETION_KWARGS={} => includes presence_penalty and extra_body."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            AI_ASSISTANT_MODEL="test-model",
            AI_ASSISTANT_BACKEND_TYPE="vllm",
            AI_ASSISTANT_COMPLETION_KWARGS={},
        ) as mock_config:
            streamer = self._make_streamer_with_config(mock_config)
            kwargs = self._call_and_get_kwargs(streamer)

        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["top_p"], 0.8)
        self.assertEqual(kwargs["presence_penalty"], 1.5)
        self.assertIn("extra_body", kwargs)
        self.assertEqual(kwargs["extra_body"]["top_k"], 20)

    def test_override_merges_with_provider(self):
        """backend=vllm, AI_ASSISTANT_COMPLETION_KWARGS={"temperature": 0.5} => temperature=0.5 + rest of vllm defaults."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            AI_ASSISTANT_MODEL="test-model",
            AI_ASSISTANT_BACKEND_TYPE="vllm",
            AI_ASSISTANT_COMPLETION_KWARGS={"temperature": 0.5},
        ) as mock_config:
            streamer = self._make_streamer_with_config(mock_config)
            kwargs = self._call_and_get_kwargs(streamer)

        self.assertEqual(kwargs["temperature"], 0.5)
        self.assertEqual(kwargs["top_p"], 0.8)
        self.assertEqual(kwargs["presence_penalty"], 1.5)

    def test_unknown_provider_uses_fallback(self):
        """backend='custom_provider', AI_ASSISTANT_COMPLETION_KWARGS={} => FALLBACK_DEFAULTS applied."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            AI_ASSISTANT_MODEL="test-model",
            AI_ASSISTANT_BACKEND_TYPE="custom_provider",
            AI_ASSISTANT_COMPLETION_KWARGS={},
        ) as mock_config:
            streamer = self._make_streamer_with_config(mock_config)
            kwargs = self._call_and_get_kwargs(streamer)

        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["top_p"], 0.8)
        self.assertNotIn("presence_penalty", kwargs)

    def test_protected_keys_ignored(self):
        """AI_ASSISTANT_COMPLETION_KWARGS with protected keys are ignored and a warning is logged."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            AI_ASSISTANT_MODEL="real-model",
            AI_ASSISTANT_BACKEND_TYPE="ollama",
            AI_ASSISTANT_COMPLETION_KWARGS={"model": "evil-model", "temperature": 0.5},
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
        """AI_ASSISTANT_COMPLETION_KWARGS={} => pure provider defaults, no extra keys."""
        with patch(
            "waldur_mastermind.chat.llm_streamer.config",
            AI_ASSISTANT_MODEL="test-model",
            AI_ASSISTANT_BACKEND_TYPE="openai",
            AI_ASSISTANT_COMPLETION_KWARGS={},
        ) as mock_config:
            streamer = self._make_streamer_with_config(mock_config)
            kwargs = self._call_and_get_kwargs(streamer)

        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["top_p"], 0.8)
        self.assertNotIn("presence_penalty", kwargs)
        self.assertNotIn("extra_body", kwargs)


@patch(SYNC_THREAD_PATCH, _SynchronousThread)
class LLMStreamerCancelViaDBTest(_LLMStreamerTestBase, drf_test.APITestCase):
    """Test cross-process cancellation via the cancel_requested_at DB flag."""

    def _make_thread_session(self):
        user = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=user)
        return ThreadSession.objects.create(chat_session=session, name="test")

    def test_cancel_via_db_freezes_content(self):
        """Setting cancel_requested_at on ThreadSession freezes content accumulation."""
        thread = self._make_thread_session()

        # Generate enough chunks to trigger at least one DB cancel check.
        # First _CANCEL_CHECK_INTERVAL chunks are accumulated normally,
        # then the DB check fires and content freezes.
        total_chunks = _CANCEL_CHECK_INTERVAL + 10
        chunks = [_make_chunk(content=f"w{i} ") for i in range(total_chunks)]
        chunks.append(
            _make_chunk(usage={"prompt_tokens": 100, "completion_tokens": 50})
        )

        # Set the cancel flag in DB before streaming starts
        ThreadSession.objects.filter(pk=thread.pk).update(
            cancel_requested_at=timezone.now()
        )

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            thread=thread,
        )
        streamer.client = _mock_openai_client(chunks)
        list(streamer)

        # Content should be frozen at or before the cancel check interval
        word_count = len(streamer.accumulated_content.strip().split())
        self.assertLessEqual(word_count, _CANCEL_CHECK_INTERVAL)
        # Usage was still recorded
        self.assertEqual(streamer.input_tokens, 100)
        self.assertEqual(streamer.output_tokens, 50)

    def test_cancel_flag_cleared_after_stream(self):
        """cancel_requested_at is reset to None after the worker completes."""
        thread = self._make_thread_session()

        ThreadSession.objects.filter(pk=thread.pk).update(
            cancel_requested_at=timezone.now()
        )

        chunks = [
            _make_chunk(content="Hello"),
            _make_chunk(usage={"prompt_tokens": 10, "completion_tokens": 5}),
        ]
        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            thread=thread,
        )
        streamer.client = _mock_openai_client(chunks)
        list(streamer)

        thread.refresh_from_db()
        self.assertIsNone(thread.cancel_requested_at)

    def test_persist_normal_updates_placeholder_not_insert(self):
        """_persist_normal() updates the pre-created placeholder instead of inserting a new row."""
        thread = self._make_thread_session()
        user_msg = Message.objects.create(
            thread=thread, role=Message.Role.USER, content="hi", sequence_index=1
        )
        placeholder = Message.objects.create(
            thread=thread, role=Message.Role.ASSISTANT, content="", sequence_index=2
        )

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            thread=thread,
            user_msg=user_msg,
            assistant_msg=placeholder,
        )
        streamer.client = _mock_openai_client([_make_chunk(content="Hello world")])
        list(streamer)

        placeholder.refresh_from_db()
        self.assertEqual(placeholder.content, "Hello world")
        # No extra row should have been inserted
        self.assertEqual(Message.objects.filter(thread=thread).count(), 2)

    def test_supersession_via_newer_user_message(self):
        """Worker detects supersession when a newer user message appears on the thread."""
        thread = self._make_thread_session()

        # Simulate the old stream's pre-created user message
        old_user_msg = Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            content="old question",
            sequence_index=1,
        )
        assistant_placeholder = Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            content="",
            sequence_index=2,
        )

        total_chunks = _CANCEL_CHECK_INTERVAL + 10
        chunks = [_make_chunk(content=f"w{i} ") for i in range(total_chunks)]
        chunks.append(
            _make_chunk(usage={"prompt_tokens": 100, "completion_tokens": 50})
        )

        # Simulate a new stream creating a newer user message
        Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            content="new question",
            sequence_index=3,
        )

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            thread=thread,
            user_msg=old_user_msg,
            assistant_msg=assistant_placeholder,
        )
        streamer.client = _mock_openai_client(chunks)
        list(streamer)

        # Content should be frozen at or before the cancel check interval
        word_count = len(streamer.accumulated_content.strip().split())
        self.assertLessEqual(word_count, _CANCEL_CHECK_INTERVAL)
        # Usage was still recorded (stream drained for usage-only chunk)
        self.assertEqual(streamer.input_tokens, 100)
        self.assertEqual(streamer.output_tokens, 50)

    def test_supersession_detected_even_when_cancel_flag_cleared(self):
        """The exact race: cancel flag cleared by new stream() call,
        but supersession is still detected via the newer user message."""
        thread = self._make_thread_session()

        old_user_msg = Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            content="old question",
            sequence_index=1,
        )

        # Cancel was set then cleared by new stream() call — the race condition
        thread.cancel_requested_at = None
        thread.save(update_fields=["cancel_requested_at"])

        # New stream created a newer user message
        Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            content="new question",
            sequence_index=3,
        )

        total_chunks = _CANCEL_CHECK_INTERVAL + 10
        chunks = [_make_chunk(content=f"w{i} ") for i in range(total_chunks)]
        chunks.append(_make_chunk(usage={"prompt_tokens": 50, "completion_tokens": 25}))

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            thread=thread,
            user_msg=old_user_msg,
        )
        streamer.client = _mock_openai_client(chunks)
        list(streamer)

        word_count = len(streamer.accumulated_content.strip().split())
        self.assertLessEqual(word_count, _CANCEL_CHECK_INTERVAL)

    def test_explicit_cancel_still_works_without_newer_message(self):
        """Explicit cancel (stop button) still works when there's no newer user message."""
        thread = self._make_thread_session()

        old_user_msg = Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            content="question",
            sequence_index=1,
        )

        ThreadSession.objects.filter(pk=thread.pk).update(
            cancel_requested_at=timezone.now()
        )

        total_chunks = _CANCEL_CHECK_INTERVAL + 10
        chunks = [_make_chunk(content=f"w{i} ") for i in range(total_chunks)]
        chunks.append(
            _make_chunk(usage={"prompt_tokens": 100, "completion_tokens": 50})
        )

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            thread=thread,
            user_msg=old_user_msg,
        )
        streamer.client = _mock_openai_client(chunks)
        list(streamer)

        word_count = len(streamer.accumulated_content.strip().split())
        self.assertLessEqual(word_count, _CANCEL_CHECK_INTERVAL)

    def test_no_false_positive_from_own_assistant_placeholder(self):
        """The worker's own assistant placeholder should not trigger supersession."""
        thread = self._make_thread_session()

        user_msg = Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            content="question",
            sequence_index=1,
        )
        # Own assistant placeholder (higher seq, but role=ASSISTANT)
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            content="",
            sequence_index=2,
        )

        chunks = [_make_chunk(content=f"w{i} ") for i in range(30)]
        chunks.append(
            _make_chunk(usage={"prompt_tokens": 100, "completion_tokens": 50})
        )

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            thread=thread,
            user_msg=user_msg,
        )
        streamer.client = _mock_openai_client(chunks)
        list(streamer)

        # Should NOT be cancelled — all 30 words should be accumulated
        word_count = len(streamer.accumulated_content.strip().split())
        self.assertEqual(word_count, 30)
