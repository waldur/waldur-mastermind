"""Shared test utilities for chat module tests."""

from contextlib import contextmanager
from unittest.mock import MagicMock

from waldur_mastermind.chat.block_schemas import blocks_to_text

# Patch target for _SynchronousThread — use with @mock.patch(SYNC_THREAD_PATCH, ...)
SYNC_THREAD_PATCH = "waldur_mastermind.chat.llm_streamer.threading.Thread"


def markdown_block(text: str, blk_id: str = "blk_0") -> dict:
    """Build a single complete markdown block matching the persisted shape."""
    return {
        "id": blk_id,
        "key": "markdown",
        "status": "complete",
        "content": text,
    }


def blocks_from_text(text: str) -> list[dict]:
    """Single-block wrapper for tests that only care about text content."""
    return [markdown_block(text)] if text else []


def text_from_blocks(blocks: list[dict]) -> str:
    """Test alias for block_schemas.blocks_to_text."""
    return blocks_to_text(blocks)


class _SynchronousThread:
    """Drop-in for threading.Thread that runs target in the calling thread.

    Allows LLMStreamer tests to use APITestCase instead of
    APITransactionTestCase by keeping DB operations on the test's connection.
    """

    def __init__(self, target=None, name=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


@contextmanager
def _fake_stream(chunks):
    """Context manager that mimics the OpenAI SDK stream object."""
    yield iter(chunks)


def _make_content_chunk(content):
    """Build a mock chunk with text content."""
    chunk = MagicMock()
    chunk.usage = None
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = None
    choice = MagicMock()
    choice.delta = delta
    chunk.choices = [choice]
    return chunk


def _make_usage_chunk(input_tokens, output_tokens):
    """Build a mock final usage-only chunk (no choices)."""
    chunk = MagicMock()
    chunk.choices = []
    chunk.usage = MagicMock()
    chunk.usage.prompt_tokens = input_tokens
    chunk.usage.completion_tokens = output_tokens
    return chunk


def _make_chunk(content=None, tool_calls=None, usage=None):
    """Build a mock OpenAI ChatCompletionChunk (unified form).

    Pass ``usage`` dict with 'prompt_tokens'/'completion_tokens' for a
    usage-only chunk; otherwise pass ``content`` and/or ``tool_calls``.
    """
    chunk = MagicMock()
    chunk.usage = None

    if usage is not None:
        chunk.choices = []
        chunk.usage = MagicMock()
        chunk.usage.prompt_tokens = usage.get("prompt_tokens", 0)
        chunk.usage.completion_tokens = usage.get("completion_tokens", 0)
    else:
        delta = MagicMock()
        delta.content = content
        delta.tool_calls = tool_calls
        choice = MagicMock()
        choice.delta = delta
        chunk.choices = [choice]

    return chunk


def _make_tool_call_delta(index, name=None, arguments=None, call_id=None):
    """Build a mock tool_call delta entry (as in chunk.choices[0].delta.tool_calls)."""
    tc = MagicMock()
    tc.index = index
    tc.id = call_id or ""
    tc.function = MagicMock()
    tc.function.name = name or ""
    tc.function.arguments = arguments or ""
    return tc


def _mock_openai_client(chunks):
    """Return a mock openai.OpenAI client whose stream yields the given chunks."""
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_stream(chunks)
    return client
