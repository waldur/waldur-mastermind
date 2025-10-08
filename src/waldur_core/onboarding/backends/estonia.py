"""
Estonian company registry backend implementation.

This module implements validation for Estonian companies using the official
Äriregister (Estonian Business Register) API.
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


class EstonianAriregisterBackend(CompanyRegistryBackend):
    """
    Estonian Business Register (Äriregister) backend.

    Validates company representatives using the official Estonian government
    business register API.
    """

    @classmethod
    def get_supported_countries(cls) -> set[str]:
        return {"EE"}  # Only Estonia

    @classmethod
    def get_validation_method(cls) -> str:
        return "ariregister"

    @classmethod
    def get_required_fields(cls) -> list[str]:
        return ["legal_person_identifier", "person_identifier"]

    @classmethod
    def get_priority(cls) -> int:
        return 1  # Highest priority for Estonia

    def validate_user_identity(self, user) -> tuple[bool, str]:
        """
        Validate Estonian user has civil_number (isikukood) from TARA.

        Args:
            user: User object to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not hasattr(user, "civil_number") or not user.civil_number:
            return False, (
                "Estonian personal ID (isikukood) is required for business registration. "
                "Please authenticate using TARA to provide your personal ID."
            )

        return True, ""

    def validate_company(self, request: ValidationRequest) -> ValidationResult:
        """
        Validate using Estonian Äriregister API.

        Based on the API at: https://avaandmed.ariregister.rik.ee/en/open-data-api/rights-representation-all-persons-related-company
        """
        try:
            verified_company_data = self._fetch_company_from_ariregister(
                request.legal_person_identifier
            )

            if not verified_company_data:
                return ValidationResult(
                    is_valid=False,
                    method_used=self.get_validation_method(),
                    company_data={},
                    user_roles=[],
                    raw_response={},
                    error_code="COMPANY_NOT_FOUND",
                    error_message=f"Company with registration code {request.legal_person_identifier} not found",
                )

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

        except ValueError:
            # Configuration or validation errors - let them bubble up
            # These should be caught by the validator to provide proper error messages
            raise
        except requests.RequestException as e:
            logger.error(f"Äriregister API request error: {str(e)}")
            return ValidationResult(
                is_valid=False,
                method_used=self.get_validation_method(),
                company_data={},
                user_roles=[],
                raw_response={},
                error_code="API_ERROR",
                error_message=f"Äriregister API error: {str(e)}",
            )
        except Exception as e:
            logger.exception("Unexpected error during company validation")
            return ValidationResult(
                is_valid=False,
                method_used=self.get_validation_method(),
                company_data={},
                user_roles=[],
                raw_response={},
                error_code="UNKNOWN_ERROR",
                error_message=f"An unexpected error occurred: {str(e)}",
            )

    def _fetch_company_from_ariregister(
        self, legal_person_identifier: str
    ) -> dict[str, Any] | None:
        """
        Fetch company data from Estonian Business Register API.

        Uses the API endpoint documented at:
        https://avaandmed.ariregister.rik.ee/en/open-data-api/rights-representation-all-persons-related-company
        """
        # Get configuration from Constance
        base_url = config.ONBOARDING_ARIREGISTER_BASE_URL
        timeout = config.ONBOARDING_ARIREGISTER_TIMEOUT
        username = config.ONBOARDING_ARIREGISTER_USERNAME
        password = config.ONBOARDING_ARIREGISTER_PASSWORD

        if not username or not password:
            logger.error("Äriregister credentials not configured")
            raise ValueError(
                "Äriregister API credentials not configured in Constance settings"
            )

        # Clean and validate legal_person_identifier
        legal_person_identifier = str(legal_person_identifier).strip()
        if not legal_person_identifier.isdigit():
            raise ValueError(
                f"Invalid legal person identifier format: {legal_person_identifier}"
            )

        xml_data = f"""<?xml version="1.0" encoding="UTF-8"?>
        <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:xro="http://x-road.eu/xsd/xroad.xsd" xmlns:iden="http://x-road.eu/xsd/identifiers" xmlns:prod="http://arireg.x-road.eu/producer/">
         <soapenv:Header/>
         <soapenv:Body>
         <prod:esindus_v1>
         <prod:keha>
         <prod:ariregister_kasutajanimi>{username}</prod:ariregister_kasutajanimi>
         <prod:ariregister_parool>{password}</prod:ariregister_parool>
         <prod:ariregister_valjundi_formaat>json</prod:ariregister_valjundi_formaat>
         <prod:ariregistri_kood>{legal_person_identifier}</prod:ariregistri_kood>
         <prod:keel>eng</prod:keel>
         </prod:keha>
         </prod:esindus_v1>
         </soapenv:Body>
        </soapenv:Envelope>
        """

        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": '""',
        }

        try:
            response = requests.post(
                base_url,
                data=xml_data.encode("utf-8"),
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            response_data = response.json()

            if "keha" not in response_data:
                logger.warning(
                    f"Invalid API response structure for code: {legal_person_identifier}"
                )
                return None

            # Check if companies list is empty
            keha = response_data.get("keha", {})
            ettevotjad = keha.get("ettevotjad", {})
            companies = ettevotjad.get("item", [])

            if not companies:
                logger.info(
                    f"No company found for registration code: {legal_person_identifier}"
                )
                return None

            return response_data

        except requests.exceptions.JSONDecodeError:
            logger.error(
                f"Invalid JSON response from Äriregister API for code: {legal_person_identifier}"
            )
            raise
        except Exception as e:
            logger.error(f"Error fetching company data: {str(e)}")
            raise

    def _check_authorization(
        self, civil_number: str, verified_company_data: dict[str, Any]
    ) -> tuple[bool, list[str]]:
        """
        Check if the person with given civil number is authorized to represent the company.

        Args:
            civil_number: Estonian personal ID (isikukood)
            verified_company_data: Raw response from Äriregister API

        Returns:
            Tuple of (is_authorized, list_of_roles)
        """
        verified_user_roles = []
        is_authorized = False

        try:
            # Navigate to the company data structure
            keha = verified_company_data.get("keha", {})
            ettevotjad = keha.get("ettevotjad", {})

            # Since we're fetching by registration code, there should be only one company
            companies = ettevotjad.get("item", [])
            if not companies:
                return False, []

            company = companies[0] if isinstance(companies, list) else companies

            # Get the persons (isikud) associated with this company
            isikud = company.get("isikud", {})
            persons = isikud.get("item", [])

            # Ensure persons is a list
            if isinstance(persons, dict):
                persons = [persons]

            for person in persons:
                person_code = str(person.get("fyysilise_isiku_kood", ""))

                if person_code == str(civil_number):
                    role = person.get("fyysilise_isiku_roll", "")
                    has_sole_representation_right = person.get(
                        "ainuesindusoigus_olemas", ""
                    )

                    if role:
                        verified_user_roles.append(role)

                    # "JAH" means "YES" in Estonian - person has sole representation right
                    if has_sole_representation_right == "JAH":
                        is_authorized = True

        except (KeyError, TypeError, AttributeError) as e:
            logger.error(
                f"Error parsing company data for authorization check: {str(e)}"
            )
            return False, []

        return is_authorized, verified_user_roles

    def _normalize_company_data(
        self, raw_data: dict[str, Any]
    ) -> StandardizedCompanyData:
        """Convert Äriregister response to standardized company data format using dataclass."""
        try:
            # Navigate to the company data
            keha = raw_data.get("keha", {})
            ettevotjad = keha.get("ettevotjad", {})
            companies = ettevotjad.get("item", [])

            if not companies:
                raise ValueError("No company data found in response")

            # Get the first (and should be only) company
            company = companies[0] if isinstance(companies, list) else companies

            # Extract company information
            name = company.get("arinimi", "")
            legal_person_identifier = str(company.get("ariregistri_kood", ""))
            status = company.get("staatus_tekstina", "")

            return StandardizedCompanyData(
                name=name,
                legal_person_identifier=legal_person_identifier,
                status=status,
                registry="Estonian Business Register",  # Static value atm
            )

        except (KeyError, TypeError, AttributeError, IndexError):
            return StandardizedCompanyData(
                name="Unknown Company",
                legal_person_identifier="",
                status="Error retrieving data",
                registry="Estonian Business Register",
            )


backend_registry.register_backend(EstonianAriregisterBackend)
