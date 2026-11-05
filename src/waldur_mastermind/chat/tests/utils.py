"""Shared test utilities for chat module tests."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import jsonschema
from django.core.serializers.json import DjangoJSONEncoder

from waldur_mastermind.chat.block_schemas import blocks_to_text

# Patch target for _SynchronousThread — use with @mock.patch(SYNC_THREAD_PATCH, ...)
SYNC_THREAD_PATCH = "waldur_mastermind.chat.llm_streamer.threading.Thread"


def assert_matches_output_schema(tool, result: dict) -> None:
    """Fail unless a successful result's `data`, JSON-encoded as the chat and
    MCP send it, validates against the tool's output_schema."""
    schema = tool.definition.output_schema
    assert result["type"] == "success", result
    assert schema is not None, f"{tool.name.value} has no output_schema"
    data = json.loads(json.dumps(result["data"], cls=DjangoJSONEncoder))
    jsonschema.validate(data, schema, cls=jsonschema.Draft202012Validator)


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


def _set_usage(usage, figures):
    """Fill a usage mock the way providers shape their cache figures.

    Every cache attribute is set explicitly, None included: MagicMock would
    otherwise invent one, and a provider that reports nothing sends no
    prompt_tokens_details at all. Writes arrive under LiteLLM's OpenAI-style
    ``prompt_tokens_details.cache_write_tokens`` or as the Anthropic-style
    ``cache_creation_input_tokens`` LiteLLM also puts on the usage itself.
    """
    usage.prompt_tokens = figures.get("prompt_tokens", 0)
    usage.completion_tokens = figures.get("completion_tokens", 0)
    read = figures.get("cache_read_tokens")
    write = figures.get("cache_write_tokens")
    usage.prompt_tokens_details = (
        None
        if read is None and write is None
        else SimpleNamespace(cached_tokens=read, cache_write_tokens=write)
    )
    usage.cache_creation_input_tokens = figures.get("cache_creation_input_tokens")


def _make_usage_chunk(input_tokens, output_tokens, **cache_figures):
    """Build a mock final usage-only chunk (no choices)."""
    chunk = MagicMock()
    chunk.choices = []
    chunk.usage = MagicMock()
    _set_usage(
        chunk.usage,
        {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}
        | cache_figures,
    )
    return chunk


def _make_chunk(content=None, tool_calls=None, usage=None):
    """Build a mock OpenAI ChatCompletionChunk (unified form).

    Pass ``usage`` dict with 'prompt_tokens'/'completion_tokens' (and
    optionally the cache figures ``_set_usage`` takes) for a usage-only chunk; otherwise pass
    ``content`` and/or ``tool_calls``.
    """
    chunk = MagicMock()
    chunk.usage = None

    if usage is not None:
        chunk.choices = []
        chunk.usage = MagicMock()
        _set_usage(chunk.usage, usage)
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


def _mock_openai_client_multi(streams_list):
    """Return a mock client whose create() yields each chunk-list in sequence.

    streams_list: list of chunk-lists, one per expected create() call.
    """
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _fake_stream(chunks) for chunks in streams_list
    ]
    return client
