"""D&B Norway backend — Right to Sign (RTS) lookup by name + birthDate."""

from typing import Any

from waldur_core.onboarding import enums
from waldur_core.onboarding.backends.base import (
    ValidationRequest,
    ValidationResult,
    backend_registry,
)

from .base import DnbBaseBackend


class DnbNorwayBackend(DnbBaseBackend):
    """D&B Norway — authorization via Nordic Right to Sign API.

    Unlike Sweden, the Norway RTS API takes ``{name: {firstName, lastName},
    birthDate}`` (no fødselsnummer accepted) and the response entries carry
    structured ``name`` + ``birthDate`` rather than
    ``nationalIdentificationNumber``. We send the user as a single
    signatory with signTogether=ANY and decide authorization from which
    list they land in (signatories[]=authorized, coSignatories[]/
    nonSignatories[]=not). On success, enriches company_data with
    credit-data fields (address, VAT, registration date, employees)
    that the RTS payload doesn't carry.
    """

    COUNTRY_CODE = "no"
    VALIDATION_METHOD = enums.ValidationMethod.DNB_NO
    REGISTRY_NAME = "D&B Norway"

    @staticmethod
    def get_person_identifier_from_user(user):
        return {
            "first_name": getattr(user, "first_name", "") or "",
            "last_name": getattr(user, "last_name", "") or "",
            "birth_date": (
                user.birth_date.isoformat() if getattr(user, "birth_date", None) else ""
            ),
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
                    "example": "Ola",
                },
                "last_name": {
                    "type": "string",
                    "label": "Last Name",
                    "required": True,
                    "example": "Nordmann",
                },
                "birth_date": {
                    "type": "date",
                    "label": "Date of Birth",
                    "required": True,
                    "format": "YYYY-MM-DD",
                    "example": "1980-05-17",
                },
            },
        }

    def validate_user_identity(self, user) -> tuple[bool, str]:
        missing = []
        if not getattr(user, "first_name", ""):
            missing.append("first name")
        if not getattr(user, "last_name", ""):
            missing.append("last name")
        if not getattr(user, "birth_date", None):
            missing.append("date of birth")
        if missing:
            return False, (
                f"D&B Norway requires {', '.join(missing)} on the user profile."
            )
        return True, ""

    def validate_company(self, request: ValidationRequest) -> ValidationResult:
        return self._validate_company_via_rts(request)

    # RTS plug points (name + birthDate request payload and matching) are
    # inherited from DnbBaseBackend — they are the default Nordic RTS shape,
    # shared with Finland. SE (ssn) and DK (flat name) override them instead.


backend_registry.register_backend(DnbNorwayBackend)
