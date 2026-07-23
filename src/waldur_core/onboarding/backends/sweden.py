"""
Swedish company registry backend implementation.

This module implements validation for Swedish companies using the Bolagsverket API.
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


class SwedenRegisterError(Exception):
    pass


class SwedenRegisterBackend(CompanyRegistryBackend):
    """
    Sweden Business Register (bolagsverket.se) backend.
    """

    def __init__(self):
        self.api_url = config.ONBOARDING_BOLAGSVERKET_API_URL.rstrip("/") + "/"
        self.client_id = config.ONBOARDING_BOLAGSVERKET_CLIENT_ID
        self.client_secret = config.ONBOARDING_BOLAGSVERKET_CLIENT_SECRET
        self._token: str | None = None

    @property
    def token(self):
        if getattr(self, "_token", None):
            return self._token

        token_base = config.ONBOARDING_BOLAGSVERKET_TOKEN_API_URL.rstrip("/") + "/"
        url = f"{token_base}oauth2/token"

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        body = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "foretagsinformation:read",
        }

        try:
            response = requests.post(url, headers=headers, data=body)
            response.raise_for_status()
            data = response.json()
            token = data.get("access_token")
            if not token:
                raise SwedenRegisterError("Access token is missing in response.")
            self._token = token
            return token
        except requests.exceptions.RequestException as e:
            raise SwedenRegisterError(f"Access token request failed: {e}")

    def post(self, path, params=None, data=None):
        path = path.lstrip("/")
        url = f"{self.api_url}foretagsinformation/v4/{path}"

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

        try:
            response = requests.post(url, headers=headers, params=params, json=data)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            try:
                error_data = response.json()
                error_data_detail = error_data.get("detail", str(e))
                logger.error(
                    f"Sweden Business Register API error detail: {error_data_detail}"
                )
                raise SwedenRegisterError(error_data_detail)
            except (ValueError, AttributeError):
                raise SwedenRegisterError(
                    f"Sweden Business Register API request failed: {e}"
                )

    @staticmethod
    def get_person_identifier_from_user(user):
        return getattr(user, "civil_number", "")

    @classmethod
    def get_supported_countries(cls) -> set[str]:
        return {"SE"}

    @classmethod
    def get_validation_method(cls) -> str:
        return enums.ValidationMethod.BOLAGSVERKET

    @classmethod
    def get_required_fields(cls) -> list[str]:
        return ["legal_person_identifier", "person_identifier"]

    @classmethod
    def get_person_identifier_fields(cls) -> dict[str, Any]:
        """
        Swedish backend requires civil_number (personnummer) for validation.

        Returns simple string identifier specification.
        """
        return {
            "type": "string",
            "field": "civil_number",
            "label": "Personal ID (personnummer)",
            "description": "Swedish personal identification number",
            "example": "800108-1234",
            "pattern": r"^\d{6}-?\d{4}$",
            "help_text": "Swedish personal identification number in format YYMMDD-XXXX",
        }

    @classmethod
    def get_priority(cls) -> int:
        return 1

    def validate_user_identity(self, user) -> tuple[bool, str]:
        if not hasattr(user, "civil_number") or not user.civil_number:
            return False, (
                "Sweden personal ID (personnummer) is required for business registration."
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

        except ValueError:
            # Configuration errors (e.g. missing credentials) must propagate
            # to BackendRegistry.validate_company so the fallback chain can
            # treat them as retryable CONFIGURATION_ERROR.
            raise
        except Exception as e:
            if isinstance(e, SwedenRegisterError):
                logger.error(f"Sweden Business Register API request error: {str(e)}")
                error_code = ErrorCode.API_ERROR
                error_message = f"Sweden Business Register API error: {str(e)}"
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
        data = {
            "identitetsbeteckning": register_number,
            "organisationInformationsmangd": ["FUNKTIONARER"],
        }
        response = self.post("organisationer", data=data)
        result = response.json()

        if not result or not isinstance(result, list):
            raise SwedenRegisterError(
                "Invalid response from Bolagsverket: expected a non-empty list of organizations."
            )

        return response.json()[0]

    def _check_authorization(
        self, person_identifier: str, verified_company_data: dict
    ) -> tuple:
        funktionarer = verified_company_data.get("funktionarer", [])
        person_identifier = str(person_identifier).replace("-", "").replace(" ", "")

        for person in funktionarer:
            personnummer = person.get("identitet", {}).get("identitetsbeteckning")
            if personnummer == person_identifier:
                roles = ",".join(
                    [
                        role.get("klartext", "")
                        for role in person.get("funktionarsroller", [])
                    ]
                )
                return True, roles

        return False, []

    def _normalize_company_data(self, raw_data: dict) -> dict:
        name = raw_data.get("organisationsnamn", {}).get("namn", "")
        legal_person_identifier = raw_data.get("identitet", {}).get(
            "identitetsbeteckning", ""
        )

        return dict(
            name=name,
            legal_person_identifier=legal_person_identifier,
            registry="Sweden Business Register (Bolagsverket)",
        )


backend_registry.register_backend(SwedenRegisterBackend)
