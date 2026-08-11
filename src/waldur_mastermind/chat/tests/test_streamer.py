import json
import queue
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import openai
from django.utils import timezone
from rest_framework import test as drf_test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.llm_streamer import (
    _CANCEL_CHECK_INTERVAL,
    _MAX_TOOL_ROUNDS,
    LLMStreamer,
)
from waldur_mastermind.chat.models import (
    ChatSession,
    Message,
    ThreadSession,
    TokenQuota,
)
from waldur_mastermind.chat.serializers import MessageSerializer
from waldur_mastermind.chat.tests.utils import (
    SYNC_THREAD_PATCH,
    _make_chunk,
    _make_tool_call_delta,
    _mock_openai_client,
    _mock_openai_client_multi,
    _SynchronousThread,
    blocks_from_text,
    text_from_blocks,
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

    def _make_streamer(self, chunks, messages=None, user=None, enabled_tools=None):
        streamer = LLMStreamer(
            messages or _messages(),
            "https://example.com/v1",
            "dummy-token",
            user=user,
        )
        streamer.client = _mock_openai_client(chunks)
        if enabled_tools:
            streamer._enabled_tool_names.update(enabled_tools)
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
        tc1 = _make_tool_call_delta(
            0, name="display_user_resources", call_id="call_abc"
        )
        tc2 = _make_tool_call_delta(0, arguments="{}")
        round0 = [
            _make_chunk(tool_calls=[tc1]),
            _make_chunk(tool_calls=[tc2]),
        ]
        # Round 1: text-only response so the agentic loop exits cleanly.
        round1 = [_make_chunk(content="Done.")]

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
            streamer = LLMStreamer(
                _messages(),
                "https://example.com/v1",
                "dummy-token",
                user=mock_user,
            )
            streamer.client = _mock_openai_client_multi([round0, round1])
            streamer._enabled_tool_names.add("display_user_resources")
            output = [json.loads(c) for c in streamer]

        # Tool result rendered as resource_list
        self.assertTrue(
            any(e.get("k") == "resource_list" for e in output),
            f"Did not find tool result rendered as resource_list. Output: {output}",
        )

        mock_exec.assert_called_once_with("display_user_resources", {})

    def test_streamer_stores_tool_result_for_persistence(self):
        """Tool results land in accumulated_blocks so persistence writes them."""
        tc = _make_tool_call_delta(
            0, name="display_user_resources", arguments="{}", call_id="call_abc"
        )
        round0 = [_make_chunk(tool_calls=[tc])]
        # Round 1: text-only response so the agentic loop exits cleanly.
        round1 = [_make_chunk(content="Done.")]

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
            streamer = LLMStreamer(
                _messages(),
                "https://example.com/v1",
                "dummy-token",
                user=mock_user,
            )
            streamer.client = _mock_openai_client_multi([round0, round1])
            streamer._enabled_tool_names.add("display_user_resources")
            list(streamer)  # Consume the stream

        tool_blocks = [b for b in streamer.accumulated_blocks if b["key"] == "tool"]
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]["tool"]["name"], "display_user_resources")
        self.assertEqual(tool_blocks[0]["result"]["key"], "resource_list")
        self.assertEqual(tool_blocks[0]["result"]["project_uuid"], "abc123")

    def test_followup_content_includes_code_block_text(self):
        """Pre-tool code block text must reach the follow-up assistant message content field."""
        tc = _make_tool_call_delta(
            0, name="display_user_resources", arguments="{}", call_id="call_1"
        )
        first_chunks = [
            _make_chunk(content="```python\nprint('hi')\n```"),
            _make_chunk(tool_calls=[tc]),
        ]
        second_chunks = [_make_chunk(content="Done.")]

        tool_result = {
            "type": "success",
            "summary": "ok",
            "ui_component": "resource_list",
            "ui_data": {},
        }

        mock_user = Mock()
        mock_user.id = 1
        mock_user.username = "testuser"

        with patch(
            "waldur_mastermind.chat.llm_streamer.ToolExecutor.execute_tool",
            return_value=tool_result,
        ):
            streamer = LLMStreamer(
                _messages(),
                "https://example.com/v1",
                "dummy-token",
                user=mock_user,
            )
            streamer.client = _mock_openai_client_multi([first_chunks, second_chunks])
            list(streamer)

        followup_messages = streamer.client.chat.completions.create.call_args_list[
            1
        ].kwargs["messages"]
        assistant_msg = next(
            m for m in followup_messages if m.get("role") == "assistant"
        )
        self.assertIsNotNone(assistant_msg["content"])
        self.assertIn("print", assistant_msg["content"])

    def test_tool_call_followup_two_stream_flow(self):
        """Full two-stream tool-call flow: pre-tool text, tool exec, follow-up answer."""
        tc = _make_tool_call_delta(
            0, name="display_user_resources", arguments="{}", call_id="call_42"
        )
        first_chunks = [
            _make_chunk(content="Let me check that."),
            _make_chunk(tool_calls=[tc]),
        ]
        second_chunks = [_make_chunk(content="Here are your resources.")]

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
            "waldur_mastermind.chat.llm_streamer.ToolExecutor.execute_tool",
            return_value=tool_result,
        ):
            streamer = LLMStreamer(
                _messages(),
                "https://example.com/v1",
                "dummy-token",
                user=mock_user,
            )
            streamer.client = _mock_openai_client_multi([first_chunks, second_chunks])
            output = [json.loads(c) for c in streamer]

        self.assertEqual(streamer.client.chat.completions.create.call_count, 2)

        followup_messages = streamer.client.chat.completions.create.call_args_list[
            1
        ].kwargs["messages"]
        assistant_msg = next(
            m for m in followup_messages if m.get("role") == "assistant"
        )
        self.assertEqual(assistant_msg["content"], "Let me check that.")

        markdown_chunks = [e.get("c", "") for e in output if e.get("k") == "markdown"]
        self.assertIn("Let me check that.", markdown_chunks)
        self.assertIn("Here are your resources.", markdown_chunks)

    def test_streamer_hides_tool_errors(self):
        """Tool errors are not displayed to users; only an empty placeholder is persisted."""
        tc = _make_tool_call_delta(
            0, name="unknown_tool", arguments="{}", call_id="call_x"
        )
        round0 = [_make_chunk(tool_calls=[tc])]
        # Round 1: text-only response so the agentic loop exits cleanly.
        round1 = [_make_chunk(content="Done.")]

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
            streamer = LLMStreamer(
                _messages(),
                "https://example.com/v1",
                "dummy-token",
                user=mock_user,
            )
            streamer.client = _mock_openai_client_multi([round0, round1])
            streamer._enabled_tool_names.add("unknown_tool")
            output = [json.loads(c) for c in streamer]

        for event in output:
            if "c" in event:
                self.assertNotIn("Unknown tool", event["c"])

        mock_exec.assert_called_once()

        # Error results still persist a tool block so cross-turn rehydration
        # sees the call, but the result block is the synthesized empty
        # markdown placeholder — no error message reaches the user.
        tool_blocks = [b for b in streamer.accumulated_blocks if b["key"] == "tool"]
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]["result"]["key"], "markdown")
        self.assertEqual(tool_blocks[0]["result"]["content"], "")


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
            text_from_blocks(streamer.accumulated_blocks),
            first_chunk + "beautiful " + "world",
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
        self.assertEqual(
            text_from_blocks(streamer.accumulated_blocks), first_chunk + "world"
        )
        # Usage chunk was still processed
        self.assertEqual(streamer.input_tokens, 42)
        self.assertEqual(streamer.output_tokens, 18)

    def test_disconnect_skips_tool_execution(self):
        """Tool calls are not executed when client has disconnected."""
        gate = threading.Event()
        tc1 = _make_tool_call_delta(
            0, name="display_user_resources", call_id="call_abc"
        )
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
        self.assertEqual(
            text_from_blocks(streamer.accumulated_blocks),
            "This input has been blocked.",
        )

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
        self.assertEqual(text_from_blocks(streamer.accumulated_blocks), "Hello world")

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
            # Simulate a minimal LLM response (no blocks needed — persist handles empty)

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
        self.assertEqual(text_from_blocks(streamer.accumulated_blocks), "partial")

    def test_enqueue_returns_when_client_gone(self):
        """_enqueue must unblock when the client disconnects so the worker
        thread isn't stuck on a full queue that nobody drains."""
        chunks = [_make_chunk(content="x")]
        streamer = self._make_streamer(chunks)

        # Fill the queue past capacity so put() blocks.
        try:
            while True:
                streamer._queue.put_nowait(b"filler")
        except queue.Full:
            pass

        streamer._client_gone.set()
        done = threading.Event()

        def call_enqueue():
            streamer._enqueue(b"item")
            done.set()

        t = threading.Thread(target=call_enqueue)
        t.start()
        assert done.wait(timeout=2.0), "_enqueue did not return after client gone"
        t.join(timeout=1.0)

    def test_enqueue_keeps_blocking_on_cancel_while_client_connected(self):
        """On cancel the client is still reading the stream, so _enqueue must
        keep delivering final frames (cancel marker, persisted meta, done)
        instead of silently discarding them."""
        chunks = [_make_chunk(content="x")]
        streamer = self._make_streamer(chunks)

        # Fill the queue past capacity so put() blocks.
        try:
            while True:
                streamer._queue.put_nowait(b"filler")
        except queue.Full:
            pass

        streamer._cancel_requested.set()
        done = threading.Event()

        def call_enqueue():
            streamer._enqueue(b"item")
            done.set()

        t = threading.Thread(target=call_enqueue)
        t.start()
        # Must NOT return while the client is still connected — even though
        # cancel was requested, the final frames still need to be delivered.
        assert not done.wait(timeout=1.0), (
            "_enqueue returned on cancel; final frames would be dropped"
        )

        # Drain one slot so the pending put() can complete, then the thread exits.
        streamer._queue.get_nowait()
        assert done.wait(timeout=2.0), "_enqueue did not return after queue drained"
        t.join(timeout=1.0)


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
            yield  # makes this a generator

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
        word_count = len(text_from_blocks(streamer.accumulated_blocks).strip().split())
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
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("hi"),
            sequence_index=1,
        )
        placeholder = Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=2,
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
        self.assertEqual(text_from_blocks(placeholder.blocks), "Hello world")
        # No extra row should have been inserted
        self.assertEqual(Message.objects.filter(thread=thread).count(), 2)

    def test_supersession_via_newer_user_message(self):
        """Worker detects supersession when a newer user message appears on the thread."""
        thread = self._make_thread_session()

        # Simulate the old stream's pre-created user message
        old_user_msg = Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("old question"),
            sequence_index=1,
        )
        assistant_placeholder = Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
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
            blocks=blocks_from_text("new question"),
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
        word_count = len(text_from_blocks(streamer.accumulated_blocks).strip().split())
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
            blocks=blocks_from_text("old question"),
            sequence_index=1,
        )

        # Cancel was set then cleared by new stream() call — the race condition
        thread.cancel_requested_at = None
        thread.save(update_fields=["cancel_requested_at"])

        # New stream created a newer user message
        Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("new question"),
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

        word_count = len(text_from_blocks(streamer.accumulated_blocks).strip().split())
        self.assertLessEqual(word_count, _CANCEL_CHECK_INTERVAL)

    def test_explicit_cancel_still_works_without_newer_message(self):
        """Explicit cancel (stop button) still works when there's no newer user message."""
        thread = self._make_thread_session()

        old_user_msg = Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("question"),
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

        word_count = len(text_from_blocks(streamer.accumulated_blocks).strip().split())
        self.assertLessEqual(word_count, _CANCEL_CHECK_INTERVAL)

    def test_no_false_positive_from_own_assistant_placeholder(self):
        """The worker's own assistant placeholder should not trigger supersession."""
        thread = self._make_thread_session()

        user_msg = Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("question"),
            sequence_index=1,
        )
        # Own assistant placeholder (higher seq, but role=ASSISTANT)
        Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
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
        word_count = len(text_from_blocks(streamer.accumulated_blocks).strip().split())
        self.assertEqual(word_count, 30)


class LLMStreamerModelSnapshotTest(_LLMStreamerTestBase, drf_test.APITestCase):
    """AI_ASSISTANT_MODEL is a mutable global, so the model that produced a turn
    has to be recorded on the row at write time. There are two persistence
    paths and both must record it — a miss on either leaves blanks that are
    indistinguishable from rows written before tracking existed.
    """

    def _make_thread_session(self):
        user = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=user)
        return ThreadSession.objects.create(chat_session=session, name="test")

    def test_streaming_captures_the_configured_model(self):
        streamer = self._make_streamer([_make_chunk(content="Hello")])
        list(streamer)

        self.assertEqual(streamer.model, "test-model")

    def test_create_path_records_the_model(self):
        # _persist_messages is invoked directly rather than via list(streamer):
        # the LLM path runs it on a worker thread with its own connection,
        # which cannot see rows created inside this test's transaction.
        thread = self._make_thread_session()
        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            thread=thread,
            original_input="hi",
        )
        streamer.model = "test-model"
        streamer._absorb_block({"id": "blk_0", "k": "markdown", "c": "Hello"})
        streamer._persist_messages()

        assistant = thread.messages.get(role=Message.Role.ASSISTANT)
        self.assertEqual(assistant.model, "test-model")

    def test_placeholder_path_records_the_model(self):
        # The regenerate/placeholder branch assigns onto an existing row and
        # saves with an explicit update_fields list, so a new field is dropped
        # silently unless it is added there too.
        thread = self._make_thread_session()
        user_msg = Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("hi"),
            sequence_index=1,
        )
        placeholder = Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=2,
        )

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            thread=thread,
            user_msg=user_msg,
            assistant_msg=placeholder,
        )
        streamer.model = "test-model"
        streamer._absorb_block({"id": "blk_0", "k": "markdown", "c": "Hello world"})
        streamer._persist_messages()

        placeholder.refresh_from_db()
        self.assertEqual(placeholder.model, "test-model")


class BlockAccumulatorTest(unittest.TestCase):
    """Unit tests for LLMStreamer's blocks accumulator (no LLM, no DB)."""

    def _new_streamer(self):
        # Bypass __init__ so we can unit-test the accumulator in isolation.
        return LLMStreamer.__new__(LLMStreamer)

    def test_single_markdown_chunk_yields_one_block(self):
        s = self._new_streamer()
        s.accumulated_blocks = []
        s._current_block = None
        s.accumulated_warning = ""
        s._absorb_block({"k": "markdown", "c": "hello"})
        s._finalize_current_block()
        self.assertEqual(len(s.accumulated_blocks), 1)
        self.assertEqual(s.accumulated_blocks[0]["key"], "markdown")
        self.assertEqual(s.accumulated_blocks[0]["content"], "hello")
        self.assertEqual(s.accumulated_blocks[0]["status"], "complete")

    def test_same_kind_chunks_concat_into_single_block(self):
        s = self._new_streamer()
        s.accumulated_blocks = []
        s._current_block = None
        s.accumulated_warning = ""
        s._absorb_block({"k": "markdown", "c": "hello "})
        s._absorb_block({"k": "markdown", "c": "world"})
        s._finalize_current_block()
        self.assertEqual(len(s.accumulated_blocks), 1)
        self.assertEqual(s.accumulated_blocks[0]["content"], "hello world")

    def test_different_kinds_produce_separate_blocks(self):
        s = self._new_streamer()
        s.accumulated_blocks = []
        s._current_block = None
        s.accumulated_warning = ""
        s._absorb_block({"k": "markdown", "c": "text "})
        s._absorb_block({"k": "code", "c": "print(1)", "t": "python"})
        s._absorb_block({"k": "markdown", "c": " done"})
        s._finalize_current_block()
        kinds = [b["key"] for b in s.accumulated_blocks]
        self.assertEqual(kinds, ["markdown", "code", "markdown"])
        self.assertEqual(s.accumulated_blocks[1]["tag"], "python")

    def test_ask_user_form_chunk_persisted_as_top_level_block(self):
        # Standalone ask_user_form chunks (no enclosing tool-loading block)
        # must land in accumulated_blocks so the form survives into
        # Message.blocks and rerenders correctly on thread reload.
        s = self._new_streamer()
        s.accumulated_blocks = []
        s._current_block = None
        s.accumulated_warning = ""
        s._absorb_block(
            {
                "k": "ask_user_form",
                "questions": [
                    {
                        "id": "q0",
                        "question": "Pick one",
                        "multiSelect": False,
                        "options": [
                            {"id": "q0o0", "label": "A"},
                            {"id": "q0o1", "label": "B"},
                        ],
                    }
                ],
                "context": "Need more info:",
            }
        )
        s._finalize_current_block()
        self.assertEqual(len(s.accumulated_blocks), 1)
        blk = s.accumulated_blocks[0]
        self.assertEqual(blk["key"], "ask_user_form")
        self.assertEqual(blk["status"], "complete")
        self.assertEqual(len(blk["questions"]), 1)
        self.assertEqual(blk["context"], "Need more info:")

    def test_warning_chunk_stored(self):
        s = self._new_streamer()
        s.accumulated_blocks = []
        s._current_block = None
        s.accumulated_warning = ""
        s._absorb_block({"w": "Sensitive info detected"})
        self.assertEqual(s.accumulated_warning, "Sensitive info detected")
        self.assertEqual(s.accumulated_blocks, [])

    def test_later_warning_overwrites_earlier(self):
        s = self._new_streamer()
        s.accumulated_blocks = []
        s._current_block = None
        s.accumulated_warning = ""
        s._absorb_block({"w": "first"})
        s._absorb_block({"w": "second"})
        self.assertEqual(s.accumulated_warning, "second")

    def test_tool_loading_then_result_becomes_tool_block(self):
        s = self._new_streamer()
        s.accumulated_blocks = []
        s._current_block = None
        s.accumulated_warning = ""
        s.pending_tool_calls = {
            "call_1": {
                "name": "list_vm_projects",
                "arguments": {"org": "acme"},
                "summary": "1 project",
            },
        }
        s._absorb_block({"k": "load", "t": "tool", "call_id": "call_1"})
        s._absorb_block(
            {
                "k": "vm_order",
                "status": "project_form",
                "projects": [{"name": "proj-a"}],
                "call_id": "call_1",
            }
        )
        s._finalize_current_block()
        self.assertEqual(len(s.accumulated_blocks), 1)
        blk = s.accumulated_blocks[0]
        self.assertEqual(blk["key"], "tool")
        self.assertEqual(blk["tool"]["call_id"], "call_1")
        self.assertEqual(blk["tool"]["name"], "list_vm_projects")
        self.assertEqual(blk["result"]["key"], "vm_order")
        self.assertEqual(blk["result"]["projects"], [{"name": "proj-a"}])

    def test_interleaved_text_and_tool_preserves_order(self):
        s = self._new_streamer()
        s.accumulated_blocks = []
        s._current_block = None
        s.accumulated_warning = ""
        s.pending_tool_calls = {
            "call_1": {"name": "x", "arguments": {}, "summary": "done"},
        }
        s._absorb_block({"k": "markdown", "c": "before"})
        s._absorb_block({"k": "load", "t": "tool", "call_id": "call_1"})
        s._absorb_block({"k": "markdown", "c": "result is...", "call_id": "call_1"})
        s._absorb_block({"k": "markdown", "c": "after"})
        s._finalize_current_block()
        kinds = [b["key"] for b in s.accumulated_blocks]
        self.assertEqual(kinds, ["markdown", "tool", "markdown"])
        self.assertEqual(len(s.accumulated_blocks), 3)
        # Verify the tool block correctly correlated result via call_id.
        tool_block = s.accumulated_blocks[1]
        self.assertEqual(tool_block["tool"]["call_id"], "call_1")
        self.assertEqual(tool_block["result"]["content"], "result is...")

    def test_loading_tool_block_without_result_is_dropped_on_finalize(self):
        """Hidden tool errors leave a loading tool block with no result.

        Finalizing such a block must drop it rather than persist a malformed
        `{"key": "tool", "status": "complete"}` row with no tool metadata.
        """
        s = self._new_streamer()
        s.accumulated_blocks = []
        s._current_block = None
        s.accumulated_warning = ""
        s.pending_tool_calls = {}
        s._absorb_block({"k": "markdown", "c": "before"})
        s._absorb_block({"k": "load", "t": "tool", "call_id": "call_1"})
        # No result absorb — simulates tool_block=None (hidden error path).
        s._finalize_current_block()
        kinds = [b["key"] for b in s.accumulated_blocks]
        self.assertEqual(kinds, ["markdown"])
        self.assertEqual(s.accumulated_blocks[0]["content"], "before")

    def test_loading_tool_block_dropped_when_next_tool_arrives(self):
        """A second tool load must not finalize the previous loading block."""
        s = self._new_streamer()
        s.accumulated_blocks = []
        s._current_block = None
        s.accumulated_warning = ""
        s.pending_tool_calls = {
            "call_2": {"name": "y", "arguments": {}, "summary": "done2"},
        }
        s._absorb_block({"k": "load", "t": "tool", "call_id": "call_1"})
        # tool_1 errored hidden — no result absorb
        s._absorb_block({"k": "load", "t": "tool", "call_id": "call_2"})
        s._absorb_block({"k": "markdown", "c": "answer", "call_id": "call_2"})
        s._finalize_current_block()
        # Only the second tool call produced a persisted block.
        kinds = [b["key"] for b in s.accumulated_blocks]
        self.assertEqual(kinds, ["tool"])
        self.assertEqual(s.accumulated_blocks[0]["tool"]["call_id"], "call_2")


@patch(SYNC_THREAD_PATCH, _SynchronousThread)
class LLMStreamerBlockRoundtripTest(_LLMStreamerTestBase, drf_test.APITestCase):
    """End-to-end: LLM stream -> parser -> accumulator -> DB -> serializer.

    Proves that the four supported block kinds round-trip through the full
    persistence pipeline in order, with shapes preserved. Complements the
    isolated parser, accumulator, and serializer unit tests by wiring the
    whole stack together.
    """

    def _make_thread(self, user):
        session = ChatSession.objects.create(user=user)
        return ThreadSession.objects.create(chat_session=session)

    def _pre_create_user_msg(self, thread, content):
        return Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=blocks_from_text(content),
            sequence_index=1,
        )

    def _pre_create_assistant_placeholder(self, thread, user_msg):
        return Message.objects.create(
            thread=thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=user_msg.sequence_index + 1,
        )

    def test_all_block_kinds_roundtrip_through_persistence_and_serializer(self):
        """Markdown, mermaid, tool, and markdown blocks survive the full pipeline.

        The stream yields content-then-tool_calls-then-content. The streamer
        processes content inline via the parser but defers tool execution until
        the stream is fully drained, so the final on-disk order is:
        markdown -> mermaid -> trailing markdown -> tool. This test pins that
        order and verifies it round-trips through DB persistence and the DRF
        serializer untouched.

        Note: if the streamer is ever updated to issue a tool-use continuation
        completion (standard OpenAI tool loop) so the model can respond to the
        tool result, trailing markdown would come from that second call and the
        expected order here would shift to [markdown, mermaid, tool, markdown].
        """
        user = structure_factories.UserFactory()
        thread = self._make_thread(user)
        user_msg = self._pre_create_user_msg(thread, "Show me a diagram and a table")
        assistant_msg = self._pre_create_assistant_placeholder(thread, user_msg)

        # Round 0: markdown -> mermaid fence -> tool_call deltas -> trailing markdown.
        # Round 1: empty follow-up so the agentic loop exits (resource_list is not terminal).
        round0 = [
            _make_chunk(content="Here is a diagram:\n"),
            _make_chunk(content="```mermaid\ngraph TD\nA-->B\n```\n"),
            _make_chunk(
                tool_calls=[
                    _make_tool_call_delta(
                        0, name="display_user_resources", call_id="call_abc"
                    )
                ]
            ),
            _make_chunk(tool_calls=[_make_tool_call_delta(0, arguments="{}")]),
            _make_chunk(content="All done."),
            _make_chunk(usage={"prompt_tokens": 10, "completion_tokens": 5}),
        ]
        round1 = []

        tool_result = {
            "type": "success",
            "summary": "1 project",
            "ui_component": "resource_list",
            "ui_data": {"project_uuid": "abc123"},
        }

        with (
            patch(
                "waldur_mastermind.chat.llm_streamer.openai.OpenAI"
            ) as mock_openai_cls,
            patch(
                "waldur_mastermind.chat.llm_streamer.ToolExecutor.execute_tool"
            ) as mock_exec,
        ):
            mock_openai_cls.return_value = _mock_openai_client_multi([round0, round1])
            mock_exec.return_value = tool_result
            streamer = LLMStreamer(
                [{"role": "user", "content": "Show me a diagram and a table"}],
                "https://llm/stream",
                "tok",
                user=user,
                thread=thread,
                original_input="Show me a diagram and a table",
                user_msg=user_msg,
                assistant_msg=assistant_msg,
            )
            streamer._enabled_tool_names.add("display_user_resources")
            list(streamer)

        assistant_msg.refresh_from_db()

        kinds = [b["key"] for b in assistant_msg.blocks]
        self.assertEqual(
            kinds,
            ["markdown", "mermaid", "markdown", "tool"],
            f"Unexpected block order; got blocks={assistant_msg.blocks}",
        )
        # No guards configured in this path, so warning must be empty.
        self.assertEqual(assistant_msg.warning, "")

        tool_block = assistant_msg.blocks[3]
        self.assertEqual(tool_block["tool"]["name"], "display_user_resources")
        self.assertEqual(tool_block["result"]["key"], "resource_list")
        self.assertEqual(tool_block["result"]["project_uuid"], "abc123")

        data = MessageSerializer(assistant_msg).data
        # Full round-trip: serialized blocks equal persisted blocks.
        self.assertEqual(data["blocks"], assistant_msg.blocks)
        self.assertEqual(data["warning"], "")


class StreamerAgenticLoopTest(_LLMStreamerTestBase, unittest.TestCase):
    """The streamer should chain tool calls across multiple rounds until
    the LLM produces plain text, capped at _MAX_TOOL_ROUNDS."""

    def _make_user(self):
        user = Mock()
        user.id = 1
        user.username = "testuser"
        return user

    def _make_tool_chunks(self, tool_name, call_id, arguments="{}"):
        """Build a round that returns a single tool_call delta."""
        tc = _make_tool_call_delta(
            0, name=tool_name, call_id=call_id, arguments=arguments
        )
        return [_make_chunk(tool_calls=[tc])]

    def _make_text_chunks(self, text):
        """Build a round that returns plain text."""
        return [_make_chunk(content=text)]

    def test_chains_tool_calls_until_text_response(self):
        """search_offerings -> compare_offerings -> text should work in one turn."""
        round0 = self._make_tool_chunks("search_offerings", "call_1")
        round1 = self._make_tool_chunks("compare_offerings", "call_2")
        round2 = self._make_text_chunks("Here is the comparison")

        tool_result = {
            "type": "success",
            "summary": "ok",
            "ui_component": "resource_list",
            "ui_data": {},
        }

        mock_user = self._make_user()

        with patch(
            "waldur_mastermind.chat.llm_streamer.ToolExecutor.execute_tool",
            return_value=tool_result,
        ):
            streamer = LLMStreamer(
                _messages(),
                "https://example.com/v1",
                "dummy-token",
                user=mock_user,
            )
            streamer.client = _mock_openai_client_multi([round0, round1, round2])
            streamer._enabled_tool_names.update(
                {"search_offerings", "compare_offerings"}
            )
            list(streamer)

        self.assertEqual(streamer.client.chat.completions.create.call_count, 3)

        tool_blocks = [b for b in streamer.accumulated_blocks if b["key"] == "tool"]
        markdown_blocks = [
            b for b in streamer.accumulated_blocks if b["key"] == "markdown"
        ]
        self.assertEqual(len(tool_blocks), 2)
        self.assertEqual(len(markdown_blocks), 1)
        self.assertIn("Here is the comparison", markdown_blocks[0]["content"])

    def test_stops_at_max_tool_rounds_with_text_fallback(self):
        """If the LLM keeps tool-calling past the cap, force a final text-only call."""
        # 6 tool rounds — one more than the cap (_MAX_TOOL_ROUNDS = 5)
        tool_rounds = [
            self._make_tool_chunks("search_offerings", f"call_{i}")
            for i in range(_MAX_TOOL_ROUNDS + 1)
        ]
        # The final forced text call returns narration
        text_round = self._make_text_chunks("Here is a summary.")

        tool_result = {
            "type": "success",
            "summary": "ok",
            "ui_component": "resource_list",
            "ui_data": {},
        }

        mock_user = self._make_user()

        with patch(
            "waldur_mastermind.chat.llm_streamer.ToolExecutor.execute_tool",
            return_value=tool_result,
        ):
            streamer = LLMStreamer(
                _messages(),
                "https://example.com/v1",
                "dummy-token",
                user=mock_user,
            )
            # _MAX_TOOL_ROUNDS rounds of tool calls + 1 forced text call
            streamer.client = _mock_openai_client_multi(
                tool_rounds[:_MAX_TOOL_ROUNDS] + [text_round]
            )
            streamer._enabled_tool_names.add("search_offerings")
            list(streamer)

        # Exactly _MAX_TOOL_ROUNDS tool rounds + 1 forced final text call
        self.assertEqual(
            streamer.client.chat.completions.create.call_count,
            _MAX_TOOL_ROUNDS + 1,
        )

        # Last call must NOT include tools
        last_kwargs = streamer.client.chat.completions.create.call_args_list[-1].kwargs
        self.assertNotIn("tools", last_kwargs)

    def test_cancellation_exits_loop(self):
        """Cancellation after round 0 prevents further tool rounds.

        Round 0 runs (one create() call), then after the stream returns the
        if self._stopped check aborts the loop before round 1.
        """
        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            user=self._make_user(),
        )
        # Pre-set cancellation so _stopped is True before any work
        streamer._cancel_requested.set()
        # Wire up a mock so we can count calls if any slip through
        streamer.client = _mock_openai_client_multi(
            [self._make_text_chunks("should not appear")]
        )

        # _run_llm_workflow directly (bypasses worker thread so we stay synchronous)
        streamer._run_llm_workflow()

        # Round 0 runs once, then cancellation check aborts before round 1
        self.assertEqual(streamer.client.chat.completions.create.call_count, 1)

    def test_lazy_load_guard_uses_correct_search_tools_arg(self):
        """When the LLM calls an unloaded tool, the guard must point it at
        ``search_tools(categories=...)``, not the obsolete ``tool_names=``
        signature, and the category must match the tool's registry entry."""
        # search_offerings is registered under ToolCategory.MARKETPLACE.
        round0 = self._make_tool_chunks("search_offerings", "call_1")
        # Round 1 must terminate the loop — the guard doesn't exit early,
        # it just stashes a recovery message on the tool_call entry.
        round1 = self._make_text_chunks("ok")

        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            user=self._make_user(),
        )
        streamer.client = _mock_openai_client_multi([round0, round1])
        # Don't add search_offerings to _enabled_tool_names — guard must fire.
        streamer._run_llm_workflow()

        # tool_calls is reset between rounds; pending_tool_calls retains the
        # guard's recovery message keyed by call_id.
        summary = streamer.pending_tool_calls["call_1"]["summary"]
        self.assertIn("categories=['marketplace']", summary)
        self.assertNotIn("tool_names=", summary)

    def test_assistant_content_per_round_excludes_prior_round_text(self):
        """Across multi-round chains, each round's assistant message in the
        LLM-facing history must contain only that round's text — not all
        accumulated text from prior rounds."""
        # Each tool round prefixes a thinking line before the tool_call so
        # accumulated_blocks gains a markdown block per round.
        round0 = [
            _make_chunk(content="Looking up offerings."),
            _make_chunk(
                tool_calls=[
                    _make_tool_call_delta(0, name="search_offerings", call_id="c1")
                ]
            ),
        ]
        round1 = [
            _make_chunk(content="Now comparing."),
            _make_chunk(
                tool_calls=[
                    _make_tool_call_delta(0, name="compare_offerings", call_id="c2")
                ]
            ),
        ]
        round2 = self._make_text_chunks("Done.")

        tool_result = {
            "type": "success",
            "summary": "ok",
            "ui_component": "resource_list",
            "ui_data": {},
        }

        with patch(
            "waldur_mastermind.chat.llm_streamer.ToolExecutor.execute_tool",
            return_value=tool_result,
        ):
            streamer = LLMStreamer(
                _messages(),
                "https://example.com/v1",
                "dummy-token",
                user=self._make_user(),
            )
            streamer.client = _mock_openai_client_multi([round0, round1, round2])
            streamer._enabled_tool_names.update(
                {"search_offerings", "compare_offerings"}
            )
            list(streamer)

        # Inspect the messages handed to the second LLM call (round 1's
        # input). The most recent assistant message in that list is round 0's
        # follow-up — it should contain round 0 text only.
        round1_messages = streamer.client.chat.completions.create.call_args_list[
            1
        ].kwargs["messages"]
        round0_assistant = next(
            m for m in reversed(round1_messages) if m.get("role") == "assistant"
        )
        self.assertIn("Looking up offerings.", round0_assistant["content"])
        self.assertNotIn("Now comparing.", round0_assistant["content"])

        # Round 2's input must have a NEW assistant entry for round 1
        # whose content is round 1's text only — no round 0 bleed-through.
        round2_messages = streamer.client.chat.completions.create.call_args_list[
            2
        ].kwargs["messages"]
        round1_assistant = next(
            m for m in reversed(round2_messages) if m.get("role") == "assistant"
        )
        self.assertIn("Now comparing.", round1_assistant["content"])
        self.assertNotIn("Looking up offerings.", round1_assistant["content"])


class RehydrateFromCategoriesTest(_LLMStreamerTestBase, unittest.TestCase):
    """_rehydrate_enabled_tools must expand past-turn search_tools(categories=...) calls.

    All tests here exercise the AUTHENTICATED path — rehydration is skipped on
    the anonymous path. The helper user fixture below ensures
    _make_streamer takes the auth branch in __init__.
    """

    def setUp(self):
        super().setUp()
        # Fake authenticated user — not anonymous, not staff/support.
        # Ensures __init__ takes the auth init branch (lazy seed +
        # rehydration), not the anon branch (fixed ANONYMOUS_TOOLS).
        self._auth_user = MagicMock(
            is_anonymous=False, is_staff=False, is_support=False
        )

    def _search_tools_call(self, categories):
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "search_tools",
                        "arguments": json.dumps({"categories": categories}),
                    },
                }
            ],
        }

    def test_rehydrates_marketplace_tools_from_past_call(self):
        streamer = self._make_streamer(
            [], messages=[self._search_tools_call(["marketplace"])]
        )
        # Rehydration already ran in __init__ via _make_streamer; call it
        # again explicitly as a regression guard for idempotency.
        streamer._rehydrate_enabled_tools_from_history()
        enabled = streamer._enabled_tool_names
        self.assertIn("search_offerings", enabled)
        self.assertIn("get_offering", enabled)
        self.assertIn("list_categories", enabled)
        self.assertIn("compare_offerings", enabled)
        self.assertNotIn("create_vm", enabled)

    def test_rehydrates_multiple_categories(self):
        streamer = self._make_streamer(
            [], messages=[self._search_tools_call(["marketplace", "vm"])]
        )
        # Rehydration already ran in __init__ via _make_streamer; call it
        # again explicitly as a regression guard for idempotency.
        streamer._rehydrate_enabled_tools_from_history()
        enabled = streamer._enabled_tool_names
        self.assertIn("search_offerings", enabled)
        self.assertIn("create_vm", enabled)

    def test_rehydrates_direct_non_search_tool_call(self):
        past_call = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "create_vm", "arguments": "{}"},
                }
            ],
        }
        streamer = self._make_streamer([], messages=[past_call])
        # Rehydration already ran in __init__ via _make_streamer; call it
        # again explicitly as a regression guard for idempotency.
        streamer._rehydrate_enabled_tools_from_history()
        self.assertIn("create_vm", streamer._enabled_tool_names)

    def test_unknown_category_skipped(self):
        streamer = self._make_streamer(
            [],
            messages=[self._search_tools_call(["not_a_real_category"])],
            user=self._auth_user,
        )
        # Rehydration already ran in __init__ via _make_streamer; call it
        # again explicitly as a regression guard for idempotency.
        streamer._rehydrate_enabled_tools_from_history()
        # Only the meta-tools should remain (both are seeded so they ship
        # on turn 0 without a search_tools round).
        self.assertEqual(streamer._enabled_tool_names, {"search_tools", "ask_user"})

    def test_meta_tools_seeded_on_init(self):
        # Both ``search_tools`` and ``ask_user`` must be in the enabled set
        # after __init__, with no prior tool activity in the history.
        # ``ask_user`` is universal — the LLM needs to be able to clarify
        # before any search_tools round.
        streamer = self._make_streamer([], messages=[], user=self._auth_user)
        self.assertIn("search_tools", streamer._enabled_tool_names)
        self.assertIn("ask_user", streamer._enabled_tool_names)


class LLMStreamerAnonymousPathTest(_LLMStreamerTestBase, unittest.TestCase):
    """Anonymous-path init.

    user=None is the public anonymous chat flow. The streamer must seed the tool
    surface with ANONYMOUS_TOOLS (marketplace + ask_user, NO search_tools)
    and skip rehydration entirely.
    """

    def test_anon_init_uses_anonymous_tools(self):
        from waldur_mastermind.chat.tools.tool_sets import ANONYMOUS_TOOLS

        streamer = self._make_streamer([], messages=[], user=None)
        expected = {t.value for t in ANONYMOUS_TOOLS}
        self.assertEqual(streamer._enabled_tool_names, expected)

    def test_anon_init_excludes_search_tools(self):
        # Anonymous path uses a fixed up-front surface; search_tools
        # (the lazy-load meta-tool) is intentionally hidden so the system
        # prompt doesn't even need to mention it exists.
        streamer = self._make_streamer([], messages=[], user=None)
        self.assertNotIn("search_tools", streamer._enabled_tool_names)

    def test_anon_init_includes_ask_user(self):
        # ask_user stays in the anon surface — clarification is universal.
        streamer = self._make_streamer([], messages=[], user=None)
        self.assertIn("ask_user", streamer._enabled_tool_names)

    def test_anon_init_skips_rehydration(self):
        # Even when the messages contain prior search_tools calls (e.g. a
        # client replays history that the streamer is told to ignore for
        # the anon path), rehydration must be skipped — anon's surface is
        # fixed.
        prior_with_search_call = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_tools",
                            "arguments": '{"categories":["vm"]}',
                        },
                    }
                ],
            }
        ]
        streamer = self._make_streamer([], messages=prior_with_search_call, user=None)
        # Without skip-rehydration, this would have grown to include
        # vm tools (create_vm, plan_vm). With the skip, it stays exactly
        # at the anonymous baseline.
        self.assertNotIn("create_vm", streamer._enabled_tool_names)
        self.assertNotIn("plan_vm", streamer._enabled_tool_names)
        self.assertNotIn("search_tools", streamer._enabled_tool_names)


class LLMStreamerAnonymousToolExecutionTest(_LLMStreamerTestBase, unittest.TestCase):
    """Anonymous (user=None) chats must actually execute tool calls.

    Regression for the early-exit bug where ``_run_llm_workflow`` returned
    after round 0 whenever ``self.user`` was None — leaving advertised
    tool calls (search_offerings, get_offering) silently dropped before
    ``_execute_tool_calls_worker`` could run.
    """

    def _tool_chunks(self, tool_name, call_id, arguments="{}"):
        tc = _make_tool_call_delta(
            0, name=tool_name, call_id=call_id, arguments=arguments
        )
        return [_make_chunk(tool_calls=[tc])]

    def _text_chunks(self, text):
        return [_make_chunk(content=text)]

    def test_anon_executes_tool_then_continues_to_text(self):
        """Round 0 emits a tool call → executor runs → round 1 narrates.

        The streamer must NOT short-circuit after round 0 just because
        the caller is anonymous.
        """
        round0 = self._tool_chunks("search_offerings", "call_1")
        round1 = self._text_chunks("Here are some offerings.")

        tool_result = {
            "type": "success",
            "summary": "ok",
            "ui_component": "resource_list",
            "ui_data": {},
        }

        with patch(
            "waldur_mastermind.chat.llm_streamer.ToolExecutor.execute_tool",
            return_value=tool_result,
        ) as mock_execute:
            streamer = LLMStreamer(
                _messages(),
                "https://example.com/v1",
                "dummy-token",
                user=None,
            )
            streamer.client = _mock_openai_client_multi([round0, round1])
            streamer._run_llm_workflow()

        # Two LLM rounds — the anon caller must not exit before round 1.
        self.assertEqual(streamer.client.chat.completions.create.call_count, 2)
        # The advertised tool call must actually have been executed.
        mock_execute.assert_called_once()
        executed_tool_name = mock_execute.call_args.args[0]
        self.assertEqual(executed_tool_name, "search_offerings")

    def test_anon_cancellation_still_exits(self):
        """``self._stopped`` (cancel/disconnect) must still abort the loop —
        anonymous-fix must not regress the cancellation path.
        """
        streamer = LLMStreamer(
            _messages(),
            "https://example.com/v1",
            "dummy-token",
            user=None,
        )
        streamer._cancel_requested.set()
        streamer.client = _mock_openai_client_multi(
            [self._text_chunks("should not appear")]
        )
        streamer._run_llm_workflow()

        # Round 0 runs (one create call); cancellation aborts before round 1.
        self.assertEqual(streamer.client.chat.completions.create.call_count, 1)
