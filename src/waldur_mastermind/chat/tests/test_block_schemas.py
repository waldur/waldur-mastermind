import pytest
from rest_framework.exceptions import ValidationError

from waldur_mastermind.chat.block_schemas import (
    BlockSerializer,
    blocks_to_text,
    clean_answer_blocks,
)


def _validate(block):
    s = BlockSerializer(data=block)
    s.is_valid(raise_exception=True)
    return s.validated_data


def test_markdown_block_round_trips():
    block = {
        "id": "blk_0",
        "key": "markdown",
        "status": "complete",
        "content": "Hello **world**",
    }
    assert _validate(block) == block


def test_code_block_round_trips_with_tag():
    block = {
        "id": "blk_1",
        "key": "code",
        "status": "complete",
        "content": "print('hi')",
        "tag": "python",
    }
    assert _validate(block) == block


def test_mermaid_block_round_trips():
    block = {
        "id": "blk_2",
        "key": "mermaid",
        "status": "complete",
        "content": "graph TD\nA-->B",
    }
    assert _validate(block) == block


def test_vm_order_block_round_trips():
    block = {
        "id": "blk_4",
        "key": "vm_order",
        "status": "complete",
        "order_id": "ord-1",
        "name": "my-vm",
        "flavor": "small",
        "image": "ubuntu-22",
        "project": "proj-a",
        "organization": "acme",
        "project_uuid": "p-uuid",
        "order_status": "pending",
    }
    assert _validate(block) == block


def test_tool_block_with_structured_result_round_trips():
    block = {
        "id": "blk_5",
        "key": "tool",
        "status": "complete",
        "tool": {
            "call_id": "call_abc",
            "name": "plan_vm",
            "arguments": {"project_uuid": "abc"},
            "summary": "Preview rendered",
        },
        "result": {
            "id": "blk_5_result",
            "key": "vm_order",
            "status": "complete",
            "order_status": "preview",
            "name": "test-vm",
            "flavor": "m1.small",
            "image": "Ubuntu 22.04",
            "project": "Test Project",
            "organization": "Test Org",
        },
    }
    assert _validate(block) == block


def test_ask_user_form_block_round_trips_with_questions_and_context():
    block = {
        "id": "blk_6",
        "key": "ask_user_form",
        "status": "complete",
        "questions": [
            {
                "id": "q0",
                "question": "What's your workload?",
                "header": "Workload",
                "multiSelect": False,
                "allowFreeText": True,
                "options": [
                    {
                        "id": "q0o0",
                        "label": "Training",
                        "description": "ML Training",
                        "value": "train",
                    },
                    {
                        "id": "q0o1",
                        "label": "Inference",
                        "description": "ML Inference",
                        "value": "infer",
                    },
                ],
            }
        ],
        "context": "To recommend an offering, I need:",
    }
    assert _validate(block) == block


def test_ask_user_form_block_without_context_round_trips():
    # `context` is optional; the form must persist either way.
    block = {
        "id": "blk_7",
        "key": "ask_user_form",
        "status": "complete",
        "questions": [
            {
                "id": "q0",
                "question": "Pick a hostname",
                "multiSelect": False,
            }
        ],
    }
    assert _validate(block) == block


def test_invalid_key_rejected():
    with pytest.raises(ValidationError):
        _validate({"id": "blk_0", "key": "garbage", "status": "complete"})


def test_tool_block_missing_call_id_rejected():
    with pytest.raises(ValidationError):
        _validate(
            {
                "id": "blk_0",
                "key": "tool",
                "status": "complete",
                "tool": {"name": "x", "arguments": {}, "summary": ""},
                "result": {
                    "id": "r",
                    "key": "markdown",
                    "status": "complete",
                    "content": "x",
                },
            }
        )


def test_tool_block_with_invalid_result_rejected():
    with pytest.raises(ValidationError):
        _validate(
            {
                "id": "blk_0",
                "key": "tool",
                "status": "complete",
                "tool": {
                    "call_id": "c1",
                    "name": "x",
                    "arguments": {},
                    "summary": "",
                },
                "result": {"key": "garbage"},
            }
        )


def test_reasoning_block_round_trips():
    block = {
        "id": "blk_0",
        "key": "reasoning",
        "status": "complete",
        "content": "The user wants a greeting.",
        "duration_ms": 6200,
    }
    assert _validate(block) == block


def test_reasoning_block_requires_duration():
    with pytest.raises(ValidationError):
        _validate(
            {"id": "blk_0", "key": "reasoning", "status": "complete", "content": "x"}
        )


def test_reasoning_is_left_out_of_the_answer_text():
    blocks = [
        {
            "id": "blk_0",
            "key": "reasoning",
            "status": "complete",
            "content": "secret plan",
            "duration_ms": 10,
        },
        {"id": "blk_1", "key": "markdown", "status": "complete", "content": "Hi"},
    ]
    # blocks_to_text feeds Message.search_text and the evaluators' response_text.
    assert blocks_to_text(blocks) == "Hi"
    assert clean_answer_blocks(blocks) == blocks


def test_narration_is_its_own_sentence_in_the_answer_text():
    blocks = [
        {
            "id": "b0",
            "key": "narration",
            "status": "complete",
            "content": "Let me check.",
        },
        {
            "id": "b1",
            "key": "markdown",
            "status": "complete",
            "content": "Here you go.",
        },
    ]
    assert blocks_to_text(blocks) == "Let me check.\n\nHere you go."


def test_narration_after_other_text_starts_a_new_paragraph():
    blocks = [
        {"id": "b0", "key": "code", "status": "complete", "content": "x = 1"},
        {
            "id": "b1",
            "key": "narration",
            "status": "complete",
            "content": "Let me check.",
        },
    ]
    assert blocks_to_text(blocks) == "```\nx = 1\n```\n\nLet me check."


def test_tool_call_markup_is_stripped_from_narration():
    blocks = [
        {
            "id": "b0",
            "key": "narration",
            "status": "complete",
            "content": 'Checking <tool_call>{"name": "x"}</tool_call>',
        }
    ]
    assert clean_answer_blocks(blocks)[0]["content"] == "Checking"
