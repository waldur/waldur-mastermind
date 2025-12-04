"""
Company onboarding validation orchestrator.

This module provides the main interface for validating company representatives
during onboarding. Currently, supports Estonia with Äriregister, designed for
easy extension to additional countries.
"""

from datetime import timedelta

from constance import config
from django.utils import timezone

from waldur_core.core.models import User

from . import enums
from .backends import ValidationRequest, backend_registry
from .backends.base import ErrorCode
from .models import OnboardingVerification


class OnboardingValidator:
    def validate_company(
        self,
        user: User,
        country: str,
        legal_person_identifier: str,
        legal_name: str = "",
        existing_verification: OnboardingVerification | None = None,
        # ToDo: remove this after implementing getting user's identifier via auth methods
        person_identifier: str = "",
        first_name: str = "",
        last_name: str = "",
        birth_date: str = "",
    ) -> OnboardingVerification:
        """
        Validate that a user is authorized to represent a company.

        Args:
            user: User requesting validation
            country: ISO country code (e.g., "EE")
            legal_person_identifier: Official company registration code
            legal_name: Company name (optional, for reference)
            existing_verification: Optional existing verification to update instead of creating new

        Returns:
            OnboardingVerification record with results
        """
        expire_delta = config.ONBOARDING_VERIFICATION_EXPIRY_HOURS

        if existing_verification:
            verification = existing_verification
            verification.legal_person_identifier = legal_person_identifier
            verification.legal_name = legal_name
            verification.status = enums.VerificationStatus.PENDING
            if not verification.expires_at:
                verification.expires_at = timezone.now() + timedelta(hours=expire_delta)
            verification.save()
        else:
            verification = OnboardingVerification.objects.create(
                user=user,
                country=country,
                legal_person_identifier=legal_person_identifier,
                legal_name=legal_name,
                status=enums.VerificationStatus.PENDING,
                expires_at=timezone.now() + timedelta(hours=expire_delta),
            )

        try:
            # ToDo: remove this after implementing getting user's identifier via auth methods
            # Use provided person_identifier if available, otherwise get from user
            temp_person_identifier = person_identifier or getattr(
                user, "civil_number", ""
            )
            # For Austrian validation: if first_name, last_name, birth_date are provided as workaround,
            # use a placeholder identifier so backend can be found
            # will be removed after implementing getting user's identifier via auth methods
            if not temp_person_identifier and (first_name and last_name and birth_date):
                # Use a placeholder for backend detection - actual Austrian data will be used in ValidationRequest
                temp_person_identifier = "at-placeholder"

            # Step 1: Validate user has required identity information
            # Use the backend registry to validate identity
            backend = backend_registry.find_backend_for_request(
                ValidationRequest(
                    country=country,
                    person_identifier=temp_person_identifier,
                    legal_person_identifier=legal_person_identifier,
                    legal_name=legal_name,
                )
            )

            if not backend:
                verification.status = enums.VerificationStatus.ESCALATED
                verification.error_traceback = (
                    f"No validation backend available for country {country}"
                )
                verification.error_message = ErrorCode.NO_BACKEND_AVAILABLE
                verification.validated_at = timezone.now()
                verification.save()
                return verification

            # ToDo: remove this after implementing getting user's identifier via auth methods
            # Skip identity validation if person_identifier is provided via request (workaround)
            # For Estonian validation: if person_identifier is provided
            # For Austrian validation: if first_name, last_name, birth_date are provided
            has_person_identifier_workaround = person_identifier or (
                first_name and last_name and birth_date
            )

            if not has_person_identifier_workaround:
                identity_valid, identity_error = backend.validate_user_identity(user)
                if not identity_valid:
                    verification.status = enums.VerificationStatus.ESCALATED
                    verification.error_traceback = identity_error
                    verification.error_message = "IDENTITY_VALIDATION_FAILED"
                    verification.validated_at = timezone.now()
                    verification.save()
                    return verification

            # Step 2: Create validation request
            # ToDo: remove this after implementing getting user's identifier via auth methods
            # Use provided person data if available, otherwise get from backend
            backend_person_identifier = backend.get_person_identifier_from_user(user)

            # For WirtschaftsCompass validation, use provided data or fall back to user object
            if (
                backend.get_validation_method()
                == enums.ValidationMethod.WIRTSCHAFTSCOMPASS
                and (first_name or last_name or birth_date)
            ):
                final_person_identifier = {
                    "first_name": first_name or getattr(user, "first_name", ""),
                    "last_name": last_name or getattr(user, "last_name", ""),
                    "birth_date": birth_date
                    or (
                        user.birth_date.strftime("%Y-%m-%d")
                        if hasattr(user, "birth_date") and user.birth_date
                        else ""
                    ),
                }
            # For other validations (e.g., Estonian), use provided person_identifier or fall back to backend
            elif person_identifier:
                final_person_identifier = person_identifier
            else:
                final_person_identifier = backend_person_identifier

            request = ValidationRequest(
                country=country,
                person_identifier=final_person_identifier,
                legal_person_identifier=legal_person_identifier,
                legal_name=legal_name,
            )

            # Step 3: Perform validation using backend registry
            result = backend_registry.validate_company(request)

            # Step 4: Update verification with results
            verification.validation_method = result.method_used
            verification.verified_user_roles = result.user_roles
            verification.verified_company_data = result.company_data
            verification.raw_response = result.raw_response
            verification.validated_at = timezone.now()

            if result.is_valid:
                verification.status = enums.VerificationStatus.VERIFIED
            else:
                verification.status = enums.VerificationStatus.ESCALATED
                verification.error_traceback = (
                    result.error_message
                    or "Automatic validation failed, escalated to manual verification"
                )
                verification.error_message = result.error_code or "VALIDATION_FAILED"

            verification.save()

        except ValueError as e:
            # Handle configuration errors (e.g., missing credentials)
            verification.status = enums.VerificationStatus.ESCALATED
            verification.error_traceback = str(e)
            verification.error_message = ErrorCode.CONFIGURATION_ERROR
            verification.validated_at = timezone.now()
            verification.save()

        except Exception as e:
            # Handle unexpected errors
            verification.status = enums.VerificationStatus.ESCALATED
            verification.error_traceback = (
                f"Unexpected error during validation: {str(e)}"
            )
            verification.error_message = ErrorCode.UNKNOWN_ERROR
            verification.validated_at = timezone.now()
            verification.save()

        return verification

    def get_supported_countries(self) -> list[str]:
        """Get list of countries that have validation backends available."""
        supported_countries = set()

        # Get countries from all registered backends
        for backend_class in backend_registry._backends:
            supported_countries.update(backend_class.get_supported_countries())

        return sorted(list(supported_countries))


# Global validator instance
onboarding_validator = OnboardingValidator()
