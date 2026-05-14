"""Compute structured diffs of object collections for audit logging."""

from collections.abc import Callable, Iterable
from typing import Any


def compute_collection_diff(
    old: Iterable[Any],
    new: Iterable[Any],
    *,
    identity_key: Callable[[Any], Any],
    compare_fields: list[str],
    serialize: Callable[[Any], dict],
) -> dict:
    """Compute added/removed/modified entries between two collections.

    Args:
        old: Existing collection (e.g. queryset of SecurityGroupRule rows).
        new: Desired collection (e.g. list of validated rule dicts or model
            instances). Entries without an identity (returned as ``None`` by
            ``identity_key``) are treated as new and reported under ``added``.
        identity_key: Callable returning a stable key for matching items
            across ``old`` and ``new`` (typically ``pk`` or ``backend_id``).
            Return ``None`` for entries that have no identity yet.
        compare_fields: Field names checked when deciding whether a matched
            pair counts as ``modified``.
        serialize: Callable turning an entry into a plain JSON-serializable
            dict suitable for an event ``context`` payload.

    Returns:
        ``{"added": [...], "removed": [...], "modified": [...], "summary": ...}``
        where ``modified`` entries have shape
        ``{"old": dict, "new": dict, "changed_fields": [str, ...]}``.
    """
    old_by_key: dict[Any, Any] = {}
    for item in old:
        key = identity_key(item)
        if key is not None:
            old_by_key[key] = item

    added: list[dict] = []
    modified: list[dict] = []
    seen_keys: set[Any] = set()

    for item in new:
        key = identity_key(item)
        if key is None or key not in old_by_key:
            added.append(serialize(item))
            continue
        seen_keys.add(key)
        old_item = old_by_key[key]
        old_payload = serialize(old_item)
        new_payload = serialize(item)
        changed = [
            field
            for field in compare_fields
            if old_payload.get(field) != new_payload.get(field)
        ]
        if changed:
            modified.append(
                {
                    "old": old_payload,
                    "new": new_payload,
                    "changed_fields": changed,
                }
            )

    removed = [
        serialize(item) for key, item in old_by_key.items() if key not in seen_keys
    ]

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
        },
    }
