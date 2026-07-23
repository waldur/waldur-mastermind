"""D&B Finland backend — Right to Sign (RTS) lookup by name + birthDate."""

from typing import Any

from waldur_core.onboarding import enums
from waldur_core.onboarding.backends.base import (
    ValidationRequest,
    ValidationResult,
    backend_registry,
)

from .base import DnbBaseBackend


class DnbFinlandBackend(DnbBaseBackend):
    """D&B Finland — authorization via Nordic Right to Sign API.

    Finland's RTS company-signatories endpoint takes the same ``{name:
    {firstName, lastName}, birthDate}`` signatory shape as Norway (no
    Finnish HETU is accepted), and the response entries carry structured
    ``name`` + ``birthDate``. We send the user as a single signatory with
    signTogether=ANY and decide authorization from which list they land in
    (signatories[]=authorized, coSignatories[]/nonSignatories[]=not). On
    success, enriches company_data with credit-data fields (address, VAT,
    registration date, employees) that the RTS payload doesn't carry.

    The request payload, matching, DEFAULT authorityType and the
    company-signatories endpoint are all inherited from DnbBaseBackend —
    Finland is behaviourally identical to Norway, differing only in country
    code / validation method / registry name.
    """

    COUNTRY_CODE = "fi"
    VALIDATION_METHOD = enums.ValidationMethod.DNB_FI
    REGISTRY_NAME = "D&B Finland"

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
                    "example": "Matti",
                },
                "last_name": {
                    "type": "string",
                    "label": "Last Name",
                    "required": True,
                    "example": "Virtanen",
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
                f"D&B Finland requires {', '.join(missing)} on the user profile."
            )
        return True, ""

    def validate_company(self, request: ValidationRequest) -> ValidationResult:
        return self._validate_company_via_rts(request)

    # RTS plug points (name + birthDate request payload and matching) are
    # inherited from DnbBaseBackend, shared with Norway.


backend_registry.register_backend(DnbFinlandBackend)
