from django.db import migrations, models


def _legacy_result_to_block(result: dict, blk_id: str) -> dict:
    """Frozen snapshot of block_schemas._legacy_result_to_block at the time
    0008 was authored. Do not change — fresh-DB setup depends on this
    behavior being stable across future refactors."""
    if not result:
        return {
            "id": blk_id,
            "key": "markdown",
            "status": "complete",
            "content": "",
        }
    kind = result.get("k") or result.get("key") or "markdown"
    if kind == "table":
        kind = "markdown"
    base = {"id": blk_id, "key": kind, "status": "complete"}
    if kind == "vm_order":
        for field in (
            "order_id",
            "name",
            "flavor",
            "image",
            "project",
            "organization",
            "project_uuid",
            "order_status",
            "message",
            "error",
            "flavors",
            "images",
            "projects",
            "offerings",
        ):
            if field in result:
                base[field] = result[field]
    else:
        base["content"] = result.get("c") or result.get("content") or ""
        if "tag" in result or "t" in result:
            base["tag"] = result.get("tag") or result.get("t")
    return base


def _legacy_to_blocks(content: str, tool_calls: list | None) -> list[dict]:
    """Frozen snapshot of block_schemas.legacy_to_blocks at the time 0008
    was authored. Do not change — fresh-DB setup depends on this behavior
    being stable across future refactors."""
    blocks: list[dict] = []
    idx = 0
    if content:
        blocks.append(
            {
                "id": f"blk_{idx}",
                "key": "markdown",
                "status": "complete",
                "content": content,
            }
        )
        idx += 1
    for call in tool_calls or []:
        blocks.append(
            {
                "id": f"blk_{idx}",
                "key": "tool",
                "status": "complete",
                "tool": {
                    "call_id": call.get("id", ""),
                    "name": call.get("name", ""),
                    "arguments": call.get("arguments") or {},
                    "summary": call.get("summary", ""),
                },
                "result": _legacy_result_to_block(
                    call.get("result") or {}, f"blk_{idx}_r"
                ),
            }
        )
        idx += 1
    return blocks


_BATCH_SIZE = 500


def backfill_blocks(apps, schema_editor):
    Message = apps.get_model("chat", "Message")
    batch = []
    for msg in Message.objects.iterator(chunk_size=_BATCH_SIZE):
        msg.blocks = _legacy_to_blocks(msg.content or "", msg.tool_calls or [])
        batch.append(msg)
        if len(batch) >= _BATCH_SIZE:
            Message.objects.bulk_update(batch, ["blocks"])
            batch.clear()
    if batch:
        Message.objects.bulk_update(batch, ["blocks"])


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0007_token_tracking_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="blocks",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="message",
            name="warning",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(backfill_blocks),
        migrations.RemoveField(
            model_name="message",
            name="content",
        ),
        migrations.RemoveField(
            model_name="message",
            name="tool_calls",
        ),
    ]
