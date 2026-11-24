"""Reading documents users send as YAML, or as the mapping it parses into.

``yaml.safe_load`` keeps anchors and aliases as shared references. Stored as
JSON, every reference is written out in full, so a few hundred bytes of nested
aliases become megabytes, and each further level multiplies that. Documents
read here therefore refuse anchors and aliases, which no export writes, and
are held to a size.
"""

import json

import yaml
from rest_framework import serializers


class NoAliasSafeLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` refusing anchors and aliases."""

    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise yaml.composer.ComposerError(
                None,
                None,
                "YAML anchors and aliases are not supported",
                event.start_mark,
            )
        return super().compose_node(parent, index)


def load_yaml(text: str):
    """Parse ``text`` with the safe loader, refusing anchors and aliases.

    Raises ``yaml.YAMLError`` as ``yaml.safe_load`` does.
    """
    return yaml.load(text, Loader=NoAliasSafeLoader)  # noqa: S506


def json_size(value) -> int:
    """The size in bytes of ``value`` serialized as JSON, as a JSONField
    stores it. Values JSON has no type for, such as dates, count as text."""
    return len(json.dumps(value, default=str, ensure_ascii=False).encode())


def too_large_message(name: str, max_bytes: int) -> str:
    return f"The {name} is larger than {max_bytes / (1024 * 1024):g} MB."


def load_document(value, max_bytes: int | None = None, name: str = "document"):
    """A document given as a mapping or a YAML string, as a mapping.

    YAML is read with ``load_yaml``. With ``max_bytes``, a YAML string longer
    than that is refused before it is parsed, and either form is refused if
    its JSON is larger. Raises a DRF ``ValidationError``.
    """
    if isinstance(value, str):
        if max_bytes is not None and len(value.encode()) > max_bytes:
            raise serializers.ValidationError(too_large_message(name, max_bytes))
        try:
            value = load_yaml(value)
        except yaml.YAMLError as e:
            raise serializers.ValidationError(f"Invalid YAML data: {e}")
    if not isinstance(value, dict):
        raise serializers.ValidationError("Expected a mapping.")
    if max_bytes is not None and json_size(value) > max_bytes:
        raise serializers.ValidationError(too_large_message(name, max_bytes))
    return value
