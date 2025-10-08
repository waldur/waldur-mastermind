"""
Company onboarding validation orchestrator.

This module provides the main interface for validating company representatives
during onboarding. Currently, supports Estonia with Äriregister, designed for
easy extension to additional countries.
"""

from datetime import timedelta
from typing import Any

from constance import config
from django.utils import timezone

from waldur_core.core.models import User

from .backends import ValidationRequest, backend_registry
from .enums import VerificationStatus
from .models import OnboardingVerification


class OnboardingValidator:
    def validate_company(
        self,
        user: User,
        country: str,
        legal_person_identifier: str,
        customer_data: dict[str, Any],
        legal_name: str = "",
    ) -> OnboardingVerification:
        """
        Validate that a user is authorized to represent a company.

        Args:
            user: User requesting validation
            country: ISO country code (e.g., "EE")
            legal_person_identifier: Official company registration code
            customer_data: Data for creating customer after validation
            legal_name: Company name (optional, for reference)

        Returns:
            OnboardingVerification record with results
        """
        expire_delta = config.ONBOARDING_VERIFICATION_EXPIRY_HOURS
        # Create verification record
        verification = OnboardingVerification.objects.create(
            user=user,
            country=country,
            legal_person_identifier=legal_person_identifier,
            legal_name=legal_name,
            user_submitted_customer_metadata=customer_data,
            status=VerificationStatus.PENDING,
            expires_at=timezone.now() + timedelta(hours=expire_delta),
        )

        try:
            # Step 1: Validate user has required identity information
            # Use the backend registry to validate identity
            backend = backend_registry.find_backend_for_request(
                ValidationRequest(
                    country=country,
                    person_identifier=getattr(user, "civil_number", ""),
                    legal_person_identifier=legal_person_identifier,
                    legal_name=legal_name,
                )
            )

            if not backend:
                verification.status = VerificationStatus.FAILED
                verification.error_traceback = (
                    f"No validation backend available for country {country}"
                )
                verification.error_message = "NO_BACKEND_AVAILABLE"
                verification.validated_at = timezone.now()
                verification.save()
                return verification

            identity_valid, identity_error = backend.validate_user_identity(user)
            if not identity_valid:
                verification.status = VerificationStatus.FAILED
                verification.error_traceback = identity_error
                verification.error_message = "IDENTITY_VALIDATION_FAILED"
                verification.validated_at = timezone.now()
                verification.save()
                return verification

            # Step 2: Create validation request
            request = ValidationRequest(
                country=country,
                person_identifier=getattr(user, "civil_number", ""),
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
                verification.status = VerificationStatus.VERIFIED
            else:
                verification.status = VerificationStatus.FAILED
                verification.error_traceback = (
                    result.error_message or "Validation failed"
                )
                verification.error_message = result.error_code or "VALIDATION_FAILED"

            verification.save()

        except ValueError as e:
            # Handle configuration errors (e.g., missing credentials)
            verification.status = VerificationStatus.FAILED
            verification.error_traceback = str(e)
            verification.error_message = "CONFIGURATION_ERROR"
            verification.validated_at = timezone.now()
            verification.save()

        except Exception as e:
            # Handle unexpected errors
            verification.status = VerificationStatus.FAILED
            verification.error_traceback = (
                f"Unexpected error during validation: {str(e)}"
            )
            verification.error_message = "UNKNOWN_ERROR"
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
