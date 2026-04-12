"""Shared helpers for proposal-related AI Assistant tools."""

from constance import config


def get_homeport_url() -> str:
    """Return the HomePort base URL, stripping trailing slash."""
    url = config.HOMEPORT_URL or ""
    return url.rstrip("/")


def call_detail_url(call_uuid: str) -> str:
    """Public call detail page URL."""
    return f"{get_homeport_url()}/calls/{call_uuid}/"


def proposal_detail_url(org_uuid: str, proposal_uuid: str) -> str:
    """Proposal detail page URL (within call management)."""
    return f"{get_homeport_url()}/call-management/{org_uuid}/proposals/{proposal_uuid}/"


def call_management_url(org_uuid: str) -> str:
    """Call management dashboard URL."""
    return f"{get_homeport_url()}/call-management/{org_uuid}/dashboard/"


def public_calls_url() -> str:
    """Public calls listing URL."""
    return f"{get_homeport_url()}/calls/"


def build_nav_block(links: list[dict], context: str = "") -> dict:
    """Build a homeport_nav UI block for the streaming response.

    Args:
        links: List of dicts with keys: label, url, variant (primary/secondary/info).
        context: Optional brief text shown above the links.

    Returns:
        Dict ready for NDJSON serialization as a homeport_nav block.
    """
    return {
        "k": "homeport_nav",
        "links": links,
        "context": context,
    }
