"""Block schema validators for Message.blocks JSONField.

Mirrors the frontend UIBlock type 1:1 so persisted blocks need no conversion
before reaching the frontend. Every block has id/key/status; key-specific
fields are validated by a per-kind serializer dispatched from BlockSerializer.
"""

from rest_framework import serializers

BLOCK_KINDS = (
    "markdown",
    "code",
    "mermaid",
    "vm_order",
    "resource_list",
    "homeport_nav",
    "tool",
)


class _BaseBlockSerializer(serializers.Serializer):
    id = serializers.CharField()
    key = serializers.CharField()
    status = serializers.ChoiceField(choices=["complete"])


class _TextBlockSerializer(_BaseBlockSerializer):
    """markdown / code / mermaid — all carry a `content` string."""

    content = serializers.CharField(allow_blank=True)
    tag = serializers.CharField(required=False)  # only code blocks use tag


class _VmOrderBlockSerializer(_BaseBlockSerializer):
    order_id = serializers.CharField(required=False, allow_blank=True)
    name = serializers.CharField(required=False, allow_blank=True)
    flavor = serializers.CharField(required=False, allow_blank=True)
    image = serializers.CharField(required=False, allow_blank=True)
    project = serializers.CharField(required=False, allow_blank=True)
    organization = serializers.CharField(required=False, allow_blank=True)
    project_uuid = serializers.CharField(required=False, allow_blank=True)
    order_status = serializers.CharField(required=False, allow_blank=True)
    message = serializers.CharField(required=False, allow_blank=True)
    error = serializers.CharField(required=False, allow_blank=True)
    flavors = serializers.ListField(required=False, child=serializers.DictField())
    images = serializers.ListField(required=False, child=serializers.DictField())
    projects = serializers.ListField(required=False, child=serializers.DictField())
    offerings = serializers.ListField(required=False, child=serializers.DictField())


class _ResourceListBlockSerializer(_BaseBlockSerializer):
    """Frontend marketplace-table filter hints. All fields optional."""

    project_uuid = serializers.CharField(required=False, allow_blank=True)
    customer_uuid = serializers.CharField(required=False, allow_blank=True)
    category_uuid = serializers.CharField(required=False, allow_blank=True)
    state = serializers.ListField(required=False, child=serializers.CharField())


class _HomeportNavBlockSerializer(_BaseBlockSerializer):
    """Navigation links with optional intro caption."""

    links = serializers.ListField(child=serializers.DictField())
    content = serializers.CharField(required=False, allow_blank=True)


class _ToolMetadataSerializer(serializers.Serializer):
    call_id = serializers.CharField()
    name = serializers.CharField()
    arguments = serializers.DictField()
    summary = serializers.CharField(allow_blank=True)


class _ToolBlockSerializer(_BaseBlockSerializer):
    tool = _ToolMetadataSerializer()
    # `result` is itself a block — validated recursively by BlockSerializer
    result = serializers.DictField()

    def validate_result(self, value):
        # Recursive validation via the polymorphic dispatcher.
        inner = BlockSerializer(data=value)
        inner.is_valid(raise_exception=True)
        return inner.validated_data


_KIND_TO_SERIALIZER = {
    "markdown": _TextBlockSerializer,
    "code": _TextBlockSerializer,
    "mermaid": _TextBlockSerializer,
    "vm_order": _VmOrderBlockSerializer,
    "resource_list": _ResourceListBlockSerializer,
    "homeport_nav": _HomeportNavBlockSerializer,
    "tool": _ToolBlockSerializer,
}


class BlockSerializer(serializers.Serializer):
    """Polymorphic dispatcher — validates any block by its `key`."""

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Block must be a dict.")
        key = data.get("key")
        if key not in _KIND_TO_SERIALIZER:
            raise serializers.ValidationError(
                {"key": f"Invalid block key '{key}'. Must be one of {BLOCK_KINDS}."}
            )
        inner = _KIND_TO_SERIALIZER[key](data=data)
        inner.is_valid(raise_exception=True)
        return inner.validated_data

    def to_representation(self, instance):
        # Blocks are stored as already-validated dicts mirroring the frontend
        # shape, so output passes through untouched.
        return instance


_TEXT_BLOCK_KINDS = ("markdown", "code", "mermaid")


def blocks_to_text(blocks: list[dict] | None) -> str:
    """Concatenate text from text-bearing blocks (markdown/code/mermaid).

    Code and mermaid blocks are rendered as fenced blocks, matching the way
    ``context_assembler._block_to_text`` feeds them to the LLM. Used by
    anything that needs the assistant's plain-text answer (e.g. the
    validation management command and tests).
    """
    parts: list[str] = []
    for b in blocks or []:
        key = b.get("key")
        if key not in _TEXT_BLOCK_KINDS:
            continue
        content = b.get("content", "")
        if key == "code":
            parts.append(f"```{b.get('tag', '')}\n{content}\n```")
        elif key == "mermaid":
            parts.append(f"```mermaid\n{content}\n```")
        else:
            parts.append(content)
    return "".join(parts)
