"""
Austrian company registry backend implementation.

This module implements validation for Austrian companies using the WirtschaftsCompass API.
"""

import logging
from dataclasses import dataclass
from typing import Any

import requests
from constance import config

from .base import (
    CompanyRegistryBackend,
    ValidationRequest,
    ValidationResult,
    backend_registry,
)

logger = logging.getLogger(__name__)


@dataclass
class StandardizedCompanyData:
    name: str
    legal_person_identifier: str
    status: str = ""
    registry: str = ""


class WiCoError(Exception):
    pass


class AustriaRegisterBackend(CompanyRegistryBackend):
    """
    Austrian Business Register (WirtschaftsCompass) backend.

    Validates company representatives using the WirtschaftsCompass API.
    """

    def __init__(self):
        self.api_url = config.ONBOARDING_WICO_API_URL.rstrip("/") + "/"
        self.token = config.ONBOARDING_WICO_TOKEN

    def get(self, path, params=None):
        path = path.lstrip("/")
        url = f"{self.api_url}organisation/{path}"

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            raise WiCoError(f"WiCo API request failed: {e}")

    @staticmethod
    def get_person_identifier_from_user(user):
        if user.first_name and user.last_name and user.birth_date:
            return {
                "first_name": user.first_name.strip(),
                "last_name": user.last_name.strip(),
                "birth_date": user.birth_date.strftime("%Y-%m-%d"),
            }

    @classmethod
    def get_supported_countries(cls) -> set[str]:
        return {"AT"}  # Only Austria

    @classmethod
    def get_validation_method(cls) -> str:
        return "wirtschaftscompass"

    @classmethod
    def get_required_fields(cls) -> list[str]:
        return ["legal_person_identifier", "person_identifier"]

    @classmethod
    def get_priority(cls) -> int:
        return 1  # Highest priority for Austria

    def validate_user_identity(self, user) -> tuple[bool, str]:
        """
        Validate Austrian user has required identity information.

        Args:
            user: User object to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        # For Austrian validation, we need some form of personal identification
        # This could be extended based on specific Austrian requirements
        if not (user.first_name and user.last_name and user.birth_date):
            return False, (
                "Personal identification is required for Austrian business registration. "
                "Please provide your personal data."
            )

        return True, ""

    def validate_company(self, request: ValidationRequest) -> ValidationResult:
        """
        Validate using WirtschaftsCompass API.

        Based on the API at: https://api.wirtschaftscompass.at/organisation/
        """
        try:
            # First, resolve the register number to get wico-id
            if not request.legal_person_identifier:
                raise WiCoError("Legal person identifier is required")

            try:
                wico_id = self._resolve_register_number(request.legal_person_identifier)
            except WiCoError:
                raise WiCoError(
                    f"Company with registration code {request.legal_person_identifier} not found"
                )

            try:
                verified_company_data = self._fetch_company_profile(wico_id)
            except WiCoError:
                raise WiCoError(f"Company data not found for wico-id {wico_id}")

            is_authorized, verified_user_roles = self._check_authorization(
                request.person_identifier, verified_company_data
            )

            normalized_data = self._normalize_company_data(verified_company_data)

            return ValidationResult(
                is_valid=is_authorized,
                method_used=self.get_validation_method(),
                company_data=normalized_data.__dict__,
                user_roles=verified_user_roles,
                raw_response=verified_company_data,
                error_code=None if is_authorized else "NOT_AUTHORIZED",
                error_message=None
                if is_authorized
                else f"User {request.person_identifier} is not listed as authorized representative",
            )

        except Exception as e:
            if isinstance(e, WiCoError):
                logger.error(f"WirtschaftsCompass API request error: {str(e)}")
                error_code = "API_ERROR"
                error_message = f"WirtschaftsCompass API error: {str(e)}"
            else:
                logger.exception("Unexpected error during company validation")
                error_code = "UNKNOWN_ERROR"
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

    def _resolve_register_number(self, register_number: str) -> str | None:
        register_number = str(register_number).strip()
        params = {
            "register-number": register_number,
            "register-number-type": "FN",
        }
        response = self.get("v1/resolve", params)
        return response.json().get("data", {}).get("wicoId")

    def _fetch_company_profile(self, wico_id: str) -> dict[str, Any] | None:
        params = {"load-option": "BODIES_INVESTMENTS"}
        response = self.get(f"v1/{wico_id}/company-report", params)
        return response.json()

    def _check_authorization(
        self, person_identifier: dict, verified_company_data: dict[str, Any]
    ) -> tuple[bool, list[str]]:
        """
        Check if the person is authorized to represent the company.

        Args:
            person_identifier: Personal identifier
            verified_company_data: Raw response from WirtschaftsCompass API

        Returns:
            Tuple of (is_authorized, list_of_roles)
        """
        verified_user_roles = []

        data = verified_company_data.get("data", {})
        bodies_investments = data.get("bodiesInvestments", {})

        def _find_user(investment_name_plural, investment_name):
            investments = bodies_investments.get(investment_name_plural, {})
            investment_list = investments.get(investment_name, [])

            for investment in investment_list:
                entity_reference = investment.get("entityReference", {})
                if (
                    entity_reference.get("objectType") == "NATURAL_PERSON"
                    and entity_reference.get("dateOfBirth")
                    == person_identifier["birth_date"]
                    and entity_reference.get("structuredName", {}).get("givenName")
                    == person_identifier["first_name"]
                    and entity_reference.get("structuredName", {}).get("surname")
                    == person_identifier["last_name"]
                ):
                    return investment.get("type", "")

        roles_to_check = [
            ("management", "management"),
            ("owners", "owner"),
            ("derivedLastBeneficialOwners", "derivedLastBenificialOwner"),
            ("investments", "investment"),
        ]
        verified_user_roles.extend(
            role
            for role in (
                _find_user(plural, singular) for plural, singular in roles_to_check
            )
            if role
        )

        return bool(verified_user_roles), verified_user_roles

    def _normalize_company_data(
        self, raw_data: dict[str, Any]
    ) -> StandardizedCompanyData:
        """Convert WirtschaftsCompass response to standardized company data format."""

        data = raw_data.get("data", {})
        basic_data = data.get("basicData", {})
        communication_data = basic_data.get("communicationData", {})
        master_data = basic_data.get("masterData", {})

        name = (
            communication_data.get("name", {})
            .get("currentValue", {})
            .get("content", "")
        )
        legal_person_identifier = (
            master_data.get("registerNumber", {})
            .get("currentValue", {})
            .get("registerNumber", {})
            .get("value", "")
        )

        status = raw_data.get("status", {}).get("registrationStatus", "UNKNOWN")

        return StandardizedCompanyData(
            name=name,
            legal_person_identifier=legal_person_identifier,
            status=status,
            registry="Austrian Business Register (WirtschaftsCompass)",
        )


backend_registry.register_backend(AustriaRegisterBackend)
