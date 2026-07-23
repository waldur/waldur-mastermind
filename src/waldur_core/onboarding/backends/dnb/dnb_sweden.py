"""D&B Sweden backend — Right to Sign (RTS) lookup by personnummer."""

import logging
import re
from typing import Any

from waldur_core.onboarding import enums
from waldur_core.onboarding.backends.base import (
    ValidationRequest,
    ValidationResult,
    backend_registry,
)

from .base import DnbBaseBackend

logger = logging.getLogger(__name__)

# Personnummer is returned by D&B in either 10-digit (YYMMDD-XXXX) or
# 12-digit (YYYYMMDD-XXXX) form; truncate to the trailing 10 digits so
# both compare equal.
_SE_COMPARE_DIGITS = 10


class DnbSwedenBackend(DnbBaseBackend):
    """D&B Sweden — authorization via Nordic Right to Sign API.

    The RTS API returns categorized lists per requested signatory:
    `signatories[]` (authorized), `nonSignatories[]` (associated with the
    company but not authorized), `coSignatories[]` (authorized only with
    a co-signer). We send a single signatory (the user's personnummer)
    with signTogether=ANY and look up which list it landed in.
    """

    COUNTRY_CODE = "se"
    VALIDATION_METHOD = enums.ValidationMethod.DNB_SE
    REGISTRY_NAME = "D&B Sweden"

    @staticmethod
    def get_person_identifier_from_user(user):
        return getattr(user, "civil_number", "")

    @classmethod
    def get_person_identifier_fields(cls) -> dict[str, Any]:
        return {
            "type": "string",
            "field": "civil_number",
            "label": "Personal ID (personnummer)",
            "description": "Swedish personal identification number",
            "example": "800108-1234",
            # 10-digit YYMMDD-XXXX is the canonical user-facing form; the
            # 12-digit YYYYMMDD-XXXX form is also accepted because D&B may
            # return either (matcher normalizes to last 10 digits).
            "pattern": r"^(\d{2})?\d{6}[-\s]?\d{4}$",
            "help_text": "Swedish personal identification number in format YYMMDD-XXXX",
        }

    def validate_user_identity(self, user) -> tuple[bool, str]:
        if not getattr(user, "civil_number", None):
            return False, (
                "Sweden personal ID (personnummer) is required for business "
                "registration. Please provide your personal data."
            )
        return True, ""

    # Inherit _unauthorized_message and _not_listed_message from the base;
    # the SE-specific override used to leak the user's personnummer into the
    # error string and conflated "found-but-not-authorized" with "not listed".

    def validate_company(self, request: ValidationRequest) -> ValidationResult:
        return self._validate_company_via_rts(request)

    # --- RTS plug points ---------------------------------------------

    def _build_signatory_payload(self, person_identifier) -> dict:
        return {"ssn": str(person_identifier or "")}

    def _signatory_matches(self, person_identifier, entry: dict) -> bool:
        normalized = _normalize_personnummer(person_identifier)
        if not normalized:
            return False
        member = _normalize_personnummer(entry.get("nationalIdentificationNumber", ""))
        return bool(member and member == normalized)


def _normalize_personnummer(pnr) -> str:
    # Strip non-digits and compare by the last 10 digits so 10-digit and
    # 12-digit forms normalize to the same value (D&B may return either).
    digits = re.sub(r"\D", "", str(pnr or ""))
    return digits[-_SE_COMPARE_DIGITS:]


backend_registry.register_backend(DnbSwedenBackend)
