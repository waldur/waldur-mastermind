"""D&B Denmark backend — Right to Sign (RTS) company-level lookup.

Denmark is matched by **name**: Waldur holds neither the user's CVR participant
id nor (the DK company-signatories endpoint's other accepted key) their address,
so that per-person endpoint can't be used. Instead DK queries the **company-level**
RTS endpoint with just the registration number and matches the user against the
returned signatory lists locally:

1. RTS returns the company's ``signatories[]`` / ``coSignatories[]`` /
   ``nonSignatories[]`` (each a flat ``name`` string + ``cvrId`` + ``roles``).
2. The user is name-matched against them. A single match in ``signatories[]``
   authorizes; a match in co/non-signatories is "not authorized"; no match is
   "not listed". A name matching **more than one** ``signatories[]`` entry is
   *ambiguous* (``RTS_AMBIGUITY_GUARD``) and escalates rather than guessing.

RTS is the authoritative signing-rights source, so an unambiguous signatory
match is trusted directly. Credit Information (COMPANY_INFORMATION) is pulled
only to enrich company_data (address/VAT) and is non-fatal. Anything not
auto-approved escalates (is_valid=False) — never a hard fail — so a reviewer
always gets the company data and signing rules.
"""

from typing import Any

from waldur_core.onboarding import enums
from waldur_core.onboarding.backends.base import (
    ValidationRequest,
    ValidationResult,
    backend_registry,
)

from .base import DnbBaseBackend


class DnbDenmarkBackend(DnbBaseBackend):
    """D&B Denmark — RTS company-level authorization, name-matched locally."""

    COUNTRY_CODE = "dk"
    VALIDATION_METHOD = enums.ValidationMethod.DNB_DK
    REGISTRY_NAME = "D&B Denmark"

    # DK's company-signatories endpoint requires an id or name+address per
    # signatory (which onboarding doesn't collect), so DK uses the company-level
    # endpoint instead and matches the person against the returned lists locally.
    RTS_USE_COMPANY_ENDPOINT = True
    RTS_AUTHORITY_TYPE = None
    # Name-only matching: escalate when several signatories share the name.
    RTS_AMBIGUITY_GUARD = True
    # Enrich company_data from credit-data when available (non-fatal).
    ENRICHMENT_SEGMENTS = ["COMPANY_INFORMATION"]

    @staticmethod
    def get_person_identifier_from_user(user):
        return {
            "first_name": getattr(user, "first_name", "") or "",
            "last_name": getattr(user, "last_name", "") or "",
        }

    @classmethod
    def get_person_identifier_fields(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "fields": {
                "first_name": {
                    "type": "string",
                    "label": "First Name",
                    "required": True,
                    "example": "Lars",
                },
                "last_name": {
                    "type": "string",
                    "label": "Last Name",
                    "required": True,
                    "example": "Jensen",
                },
            },
        }

    def validate_user_identity(self, user) -> tuple[bool, str]:
        if not getattr(user, "first_name", "") or not getattr(user, "last_name", ""):
            return False, (
                "D&B Denmark requires first and last name on the user profile."
            )
        return True, ""

    def validate_company(self, request: ValidationRequest) -> ValidationResult:
        return self._validate_company_via_rts(request)

    # --- RTS plug points ---------------------------------------------
    # DK uses the company-level endpoint (RTS_USE_COMPANY_ENDPOINT), so there is
    # no per-person request payload to build — only response matching below.

    def _signatory_matches(self, person_identifier, entry: dict) -> bool:
        # DK RTS response entries carry ``name`` as a flat string
        # ("Simon Katarina Bang-Kristiansøn") plus a ``cvrId`` — unlike SE/NO's
        # structured name object. Match on the flat name string (cvrId can't
        # help: we never know the user's).
        ident = person_identifier or {}
        return _name_matches(
            ident.get("first_name"), ident.get("last_name"), entry.get("name")
        )

    def _unauthorized_message(self, request: ValidationRequest) -> str:
        return (
            f"You are listed for this company in {self.REGISTRY_NAME}'s records "
            f"but do not have sole signing authority. Manual review is required."
        )


def _name_matches(first, last, candidate) -> bool:
    """Whether ``candidate`` (a flat full-name string) is the requested person.

    Requires the first token to equal the given first name and the last token
    to equal the surname (case-insensitive), tolerating middle names in
    between (e.g. "Simon Katarina Bang-Kristiansøn" matches first=Simon,
    last=Bang-Kristiansøn). Returns False when either side is missing or the
    candidate has fewer than two tokens — DK can only safely act on a full
    first+last name.
    """
    first = (first or "").strip().lower()
    last = (last or "").strip().lower()
    if not (first and last):
        return False
    tokens = str(candidate or "").strip().lower().split()
    if len(tokens) < 2:
        return False
    return tokens[0] == first and tokens[-1] == last


backend_registry.register_backend(DnbDenmarkBackend)
