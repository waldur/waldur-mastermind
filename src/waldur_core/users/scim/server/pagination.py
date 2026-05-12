"""SCIM 2.0 list-response pagination helpers (RFC 7644 §3.4.2.4)."""

from __future__ import annotations

LIST_RESPONSE_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 200


def parse_pagination(request) -> tuple[int, int]:
    """Return ``(start_index, count)`` from query params.

    ``startIndex`` is 1-based (RFC 7644). Invalid values are clamped to safe
    defaults rather than rejecting the request, matching reference servers.
    """
    try:
        start = int(request.query_params.get("startIndex", 1))
    except (TypeError, ValueError):
        start = 1
    try:
        count = int(request.query_params.get("count", DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        count = DEFAULT_PAGE_SIZE
    start = max(1, start)
    count = max(0, min(count, MAX_PAGE_SIZE))
    return start, count


def list_response(
    resources: list[dict], total: int, start_index: int, count: int
) -> dict:
    """Wrap a paginated slice as a SCIM ListResponse."""
    return {
        "schemas": [LIST_RESPONSE_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }
