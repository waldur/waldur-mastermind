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
            "name": "list_projects",
            "arguments": {"organization_uuid": "acme"},
            "summary": "Listed 14 projects",
        },
        "result": {
            "id": "blk_5_result",
            "key": "vm_order",
            "status": "complete",
            "order_status": "project_form",
            "projects": [{"name": "proj-a"}],
        },
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
