import pytest
from rest_framework.exceptions import ValidationError

from waldur_mastermind.chat.block_schemas import BlockSerializer


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
                "options": [
                    {"id": "q0o0", "label": "Training"},
                    {"id": "q0o1", "label": "Inference"},
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
