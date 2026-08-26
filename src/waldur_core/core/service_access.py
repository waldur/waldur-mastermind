"""How a deployment presents access to services.

The server-side mirror of homeport's ``src/marketplace/serviceAccessMode.ts``.
Navigation and applicant-facing wording only — the API serves the same data in
every mode.
"""

from constance import config

BOTH = "both"
CALLS_ONLY = "calls"
MARKETPLACE_ONLY = "marketplace"


def get_service_access_mode() -> str:
    """The configured mode, defaulting to ``both``.

    Matches the frontend's default, so a deployment that has not been migrated
    keeps the vocabulary it has always used rather than silently losing it.
    """
    return getattr(config, "SERVICE_ACCESS_MODE", BOTH) or BOTH


def names_calls() -> bool:
    """Whether applicant-facing copy may name calls, rounds and proposals.

    In marketplace-only mode it may not: the applicant arrives from an offering,
    never navigates to a call and sees no calls section, so those words name
    nothing they can point at. The counterpart of homeport's
    ``hasCallVocabulary()``.

    Deliberately narrow, and deliberately fails towards the call vocabulary:
    anything other than ``marketplace`` — including an unrecognised value — keeps
    the terms, because they are the domain's own everywhere else.
    """
    return get_service_access_mode() != MARKETPLACE_ONLY
