"""
Norwegian company registry backend implementation.

This module implements validation for Norwegian companies using the Brreg API.
"""

import logging
from typing import Any, cast

import requests
from constance import config

from waldur_core.onboarding import enums

from .base import (
    CompanyRegistryBackend,
    ErrorCode,
    ValidationRequest,
    ValidationResult,
    backend_registry,
)

logger = logging.getLogger(__name__)


class BrregError(Exception):
    pass


class NorwayRegisterBackend(CompanyRegistryBackend):
    """
    Norway Business Register (data.brreg.no) backend.
    """

    def __init__(self):
        self.api_url = config.ONBOARDING_BREG_API_URL.rstrip("/") + "/"

    @property
    def token(self):
        """Brreg API does not require authentication token."""
        return None

    def get(self, path, params=None):
        path = path.lstrip("/")
        url = f"{self.api_url}enhetsregisteret/api/{path}"

        headers = {
            "Accept": "application/json",
        }

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            raise BrregError(f"Breg API request failed: {e}")

    @staticmethod
    def get_person_identifier_from_user(user):
        return getattr(user, "civil_number", "")

    @classmethod
    def get_supported_countries(cls) -> set[str]:
        return {"NO"}

    @classmethod
    def get_validation_method(cls) -> str:
        return enums.ValidationMethod.BRREG

    @classmethod
    def get_required_fields(cls) -> list[str]:
        return ["legal_person_identifier", "person_identifier"]

    @classmethod
    def get_priority(cls) -> int:
        return 1

    def validate_user_identity(self, user) -> tuple[bool, str]:
        if not hasattr(user, "civil_number") or not user.civil_number:
            return False, (
                "Norway personal ID (fødselsnummer) is required for business registration."
                "Please provide your personal data."
            )

        return True, ""

    def validate_company(self, request: ValidationRequest) -> ValidationResult:
        try:
            person_identifier = request.person_identifier
            verified_company_data = self._get_verified_company_data(
                request.legal_person_identifier
            )
            is_authorized, verified_user_roles = self._check_authorization(
                person_identifier, verified_company_data
            )

            normalized_data = self._normalize_company_data(
                cast(dict[str, Any], verified_company_data)
            )

            return ValidationResult(
                is_valid=is_authorized,
                method_used=self.get_validation_method(),
                company_data=normalized_data,
                user_roles=verified_user_roles,
                raw_response=cast(dict[str, Any], verified_company_data),
                error_code=None if is_authorized else ErrorCode.NOT_AUTHORIZED,
                error_message=None
                if is_authorized
                else f"User {request.person_identifier} is not listed as authorized representative",
            )

        except Exception as e:
            if isinstance(e, BrregError):
                logger.error(f"Norway Business Register API request error: {str(e)}")
                error_code = ErrorCode.API_ERROR
                error_message = f"Norway Business Register API error: {str(e)}"
            else:
                logger.exception("Unexpected error during company validation")
                error_code = ErrorCode.NOT_AUTHORIZED
                error_message = f"An unexpected error occurred: {str(e)}"

            return ValidationResult(
                is_valid=False,
                method_used=self.get_validation_method(),
                company_data={},
                user_roles=[],
                raw_response={},
                error_code=error_code,
                error_message=error_message,
            )

    def _get_verified_company_data(self, register_number: str):
        register_number = str(register_number).strip()
        response = self.get(f"enheter/{register_number}")
        return response.json()

    def _check_authorization(
        self, person_identifier: str, verified_company_data: dict
    ) -> tuple:
        return True, []

    def _normalize_company_data(self, raw_data: dict) -> dict:
        name = raw_data.get("navn")
        legal_person_identifier = raw_data.get("organisasjonsnummer")

        return dict(
            name=name,
            legal_person_identifier=legal_person_identifier,
            registry="Norway Business Register (Brreg)",
        )


backend_registry.register_backend(NorwayRegisterBackend)
