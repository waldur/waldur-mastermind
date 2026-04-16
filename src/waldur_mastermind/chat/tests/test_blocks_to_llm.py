from waldur_mastermind.chat.context_assembler import blocks_to_llm_messages


def _mk(blocks):
    """Stand-in for a Message with only `.role` and `.blocks` attrs."""

    class _M:
        role = "assistant"

    m = _M()
    m.blocks = blocks
    return m


def test_empty_blocks_yields_empty_list():
    assert blocks_to_llm_messages(_mk([])) == []


def test_none_blocks_yields_empty_list():
    assert blocks_to_llm_messages(_mk(None)) == []


def test_single_markdown_block_yields_one_assistant_message():
    result = blocks_to_llm_messages(
        _mk(
            [
                {
                    "id": "b0",
                    "key": "markdown",
                    "status": "complete",
                    "content": "hi",
                }
            ]
        )
    )
    assert result == [{"role": "assistant", "content": "hi"}]


def test_code_block_is_fenced():
    result = blocks_to_llm_messages(
        _mk(
            [
                {
                    "id": "b0",
                    "key": "code",
                    "status": "complete",
                    "content": "print(1)",
                    "tag": "python",
                }
            ]
        )
    )
    assert result == [
        {
            "role": "assistant",
            "content": "```python\nprint(1)\n```",
        }
    ]


def test_mermaid_block_is_fenced_as_mermaid():
    result = blocks_to_llm_messages(
        _mk(
            [
                {
                    "id": "b0",
                    "key": "mermaid",
                    "status": "complete",
                    "content": "graph TD\nA-->B",
                }
            ]
        )
    )
    assert result == [
        {
            "role": "assistant",
            "content": "```mermaid\ngraph TD\nA-->B\n```",
        }
    ]


def test_text_before_tool_is_combined_onto_tool_calls_message():
    blocks = [
        {"id": "b0", "key": "markdown", "status": "complete", "content": "before"},
        {
            "id": "b1",
            "key": "tool",
            "status": "complete",
            "tool": {
                "call_id": "c1",
                "name": "list_projects",
                "arguments": {"org": "acme"},
                "summary": "2 projects",
            },
            "result": {
                "id": "b1r",
                "key": "vm_order",
                "status": "complete",
                "order_status": "project_form",
                "projects": [{"name": "a"}],
            },
        },
        {"id": "b2", "key": "markdown", "status": "complete", "content": "after"},
    ]
    result = blocks_to_llm_messages(_mk(blocks))
    assert result == [
        {
            "role": "assistant",
            "content": "before",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "list_projects",
                        "arguments": '{"org": "acme"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "2 projects"},
        {"role": "assistant", "content": "after"},
    ]


def test_multiple_text_blocks_get_joined_with_double_newline():
    blocks = [
        {"id": "b0", "key": "markdown", "status": "complete", "content": "para1"},
        {"id": "b1", "key": "markdown", "status": "complete", "content": "para2"},
    ]
    result = blocks_to_llm_messages(_mk(blocks))
    assert result == [{"role": "assistant", "content": "para1\n\npara2"}]


def test_tool_without_trailing_text_still_emits_both_entries():
    blocks = [
        {
            "id": "b0",
            "key": "tool",
            "status": "complete",
            "tool": {
                "call_id": "c1",
                "name": "x",
                "arguments": {},
                "summary": "done",
            },
            "result": {
                "id": "b0r",
                "key": "markdown",
                "status": "complete",
                "content": "ok",
            },
        }
    ]
    result = blocks_to_llm_messages(_mk(blocks))
    assert len(result) == 2
    assert result[0]["tool_calls"][0]["function"]["name"] == "x"
    assert result[0]["content"] is None
    assert result[1] == {"role": "tool", "tool_call_id": "c1", "content": "done"}


def test_user_role_with_markdown_block():
    class _M:
        role = "user"

    m = _M()
    m.blocks = [
        {"id": "b0", "key": "markdown", "status": "complete", "content": "question"},
    ]
    assert blocks_to_llm_messages(m) == [{"role": "user", "content": "question"}]


def test_malformed_tool_block_missing_tool_metadata_is_skipped():
    """A tool block persisted without its `tool` sub-dict must not crash.

    Legacy bug: before `_finalize_current_block` dropped loading tool blocks
    that never received a result, hidden tool errors persisted a block shaped
    `{"key": "tool", "status": "complete"}` with no tool metadata.  Reading
    such a block must not raise — the block is dropped from LLM context.
    """
    blocks = [
        {"id": "b0", "key": "markdown", "status": "complete", "content": "before"},
        {"id": "b1", "key": "tool", "status": "complete"},
        {"id": "b2", "key": "markdown", "status": "complete", "content": "after"},
    ]
    result = blocks_to_llm_messages(_mk(blocks))
    # Malformed tool block skipped — text flows through as a single message.
    assert result == [{"role": "assistant", "content": "before\n\nafter"}]


def test_malformed_tool_block_with_empty_tool_subdict_is_skipped():
    """A tool block with an empty `tool` dict is also malformed."""
    blocks = [
        {"id": "b0", "key": "tool", "status": "complete", "tool": {}, "result": {}},
    ]
    result = blocks_to_llm_messages(_mk(blocks))
    assert result == []


def test_malformed_tool_block_missing_call_id_is_skipped():
    """Tool metadata without call_id cannot round-trip through OpenAI format."""
    blocks = [
        {
            "id": "b0",
            "key": "tool",
            "status": "complete",
            "tool": {"name": "x", "arguments": {}, "summary": "done"},
            "result": {
                "id": "b0r",
                "key": "markdown",
                "status": "complete",
                "content": "ok",
            },
        }
    ]
    result = blocks_to_llm_messages(_mk(blocks))
    assert result == []


def test_tool_block_without_preceding_text_sets_content_none():
    """Matches the OpenAI canonical shape: assistant messages with tool_calls
    and no accompanying text have content=None, not absent."""
    blocks = [
        {
            "id": "b0",
            "key": "tool",
            "status": "complete",
            "tool": {
                "call_id": "c1",
                "name": "x",
                "arguments": {},
                "summary": "done",
            },
            "result": {
                "id": "b0r",
                "key": "markdown",
                "status": "complete",
                "content": "ok",
            },
        }
    ]
    result = blocks_to_llm_messages(_mk(blocks))
    assert result[0] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "x", "arguments": "{}"},
            }
        ],
    }
    assert result[1] == {"role": "tool", "tool_call_id": "c1", "content": "done"}
