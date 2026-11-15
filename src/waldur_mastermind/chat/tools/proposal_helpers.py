"""Shared helpers for proposal-related AI Assistant tools."""

from waldur_core.configuration import config
from waldur_mastermind.proposal.applicant_visibility import ApplicantVisibility

# How each applicant attribute a tool may return is read from the applicant
# (the proposal's ``created_by``). Like ``APPLICANT_FIELD_MAP`` for the REST
# payloads, a tool names the attribute each of its output keys carries, so an
# applicant detail cannot reach a tool's result without going through the
# call's applicant visibility config.
APPLICANT_ATTRIBUTES = {
    "full_name": lambda applicant: applicant.full_name,
    "organization": lambda applicant: applicant.organization,
}


def applicant_details(
    proposal, visibility: ApplicantVisibility, keys: dict[str, str]
) -> dict:
    """The applicant details of ``proposal`` that ``visibility``'s user reads.

    ``keys`` maps each output key to the applicant attribute it carries, e.g.
    ``{"created_by": "full_name"}``. A key whose attribute the call's
    applicant visibility config conceals from the user is left out, as the
    proposal API drops it, so a reviewer gets the same applicant details here
    as from the proposal itself.
    """
    concealed = visibility.concealed_attributes(proposal) or set()
    applicant = proposal.created_by
    return {
        key: (APPLICANT_ATTRIBUTES[attribute](applicant) or "") if applicant else ""
        for key, attribute in keys.items()
        if attribute not in concealed
    }


def get_homeport_url() -> str:
    """Return the HomePort base URL, stripping trailing slash."""
    url = config.HOMEPORT_URL or ""
    return url.rstrip("/")


def call_detail_url(call_uuid: str) -> str:
    """Public call detail page URL."""
    return f"{get_homeport_url()}/calls/{call_uuid}/"


def proposal_detail_url(proposal_uuid: str) -> str:
    """Proposal detail page URL."""
    return f"{get_homeport_url()}/proposals/{proposal_uuid}/"


def call_management_url(org_uuid: str) -> str:
    """Call management dashboard URL."""
    return f"{get_homeport_url()}/call-management/{org_uuid}/dashboard/"


def public_calls_url() -> str:
    """Public calls listing URL."""
    return f"{get_homeport_url()}/calls/"


def build_nav_block(links: list[dict], content: str = "") -> dict:
    """Build a homeport_nav UI block for the streaming response.

    Args:
        links: List of dicts with keys: label, url, variant (primary/secondary/info).
        content: Optional brief caption shown above the links.

    Returns:
        Dict ready for NDJSON serialization as a homeport_nav block.
    """
    return {
        "k": "homeport_nav",
        "links": links,
        "content": content,
    }
