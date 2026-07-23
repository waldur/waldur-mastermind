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
from .backends.base import ErrorCode, ValidationResult
from .models import OnboardingVerification


class OnboardingValidator:
    def validate_company(
        self,
        user: User,
        validation_method: str,
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
        Validate that a user is authorized to represent a company using specified validation method.

        Args:
            user: User requesting validation
            validation_method: Validation method name (e.g., "ariregister", "wirtschaftscompass")
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
                validation_method=validation_method,
                legal_person_identifier=legal_person_identifier,
                legal_name=legal_name,
                status=enums.VerificationStatus.PENDING,
                expires_at=timezone.now() + timedelta(hours=expire_delta),
            )

        try:
            # Step 1: Find backend by validation_method
            backend = backend_registry.find_backend_by_method(validation_method)

            if not backend:
                verification.status = enums.VerificationStatus.ESCALATED
                verification.error_traceback = (
                    f"No validation backend available for method '{validation_method}'"
                )
                verification.error_message = ErrorCode.NO_BACKEND_AVAILABLE
                verification.validated_at = timezone.now()
                verification.save()
                return verification

            # Get country from backend for context
            supported_countries = backend.get_supported_countries()
            country = list(supported_countries)[0] if supported_countries else ""
            if country:
                verification.country = country
                verification.save()

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

            # Step 2: Validate user has required identity information.
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

            # Step 3: Create validation request
            # ToDo: remove this after implementing getting user's identifier via auth methods
            # Use provided person data if available, otherwise get from backend
            backend_person_identifier = backend.get_person_identifier_from_user(user)

            # For backends with an object-shaped person identifier (Austrian
            # WirtschaftsCompass, D&B Norway, …), build the dict from form
            # input — falling back to the user profile per-field — so the
            # wizard's first_name/last_name/birth_date are never silently
            # discarded when the profile is sparse. String-identifier
            # backends (e.g. Estonian civil_number, Swedish personnummer)
            # continue to use `person_identifier` directly.
            identifier_fields = backend.get_person_identifier_fields()
            is_object_identifier = (
                isinstance(identifier_fields, dict)
                and identifier_fields.get("type") == "object"
            )

            if is_object_identifier and (first_name or last_name or birth_date):
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

            # Step 4: Perform validation using the chosen backend.
            # Dispatching directly (instead of through the registry) ensures the
            # user's selected validation_method is honoured when a country has
            # multiple parallel providers (e.g. SE: bolagsverket + dnb_se).
            if not backend.can_handle_request(request):
                result = ValidationResult(
                    is_valid=False,
                    method_used=backend.get_validation_method(),
                    company_data={},
                    user_roles=[],
                    raw_response={},
                    error_code=ErrorCode.NO_BACKEND_AVAILABLE,
                    error_message=(
                        f"Backend '{backend.get_validation_method()}' cannot "
                        f"handle the request (missing required fields or "
                        f"unsupported country)."
                    ),
                )
            else:
                try:
                    result = backend.validate_company(request)
                except ValueError:
                    # Configuration errors propagate to the outer handler below.
                    raise
                except Exception as e:
                    result = ValidationResult(
                        is_valid=False,
                        method_used=backend.get_validation_method(),
                        company_data={},
                        user_roles=[],
                        raw_response={},
                        error_code=ErrorCode.API_ERROR,
                        error_message=f"Backend error: {str(e)}",
                    )

            # Step 5: Update verification with results
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
            # Handle configuration errors (e.g., missing credentials) bubbled up
            # from the backend or from request-construction code paths.
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
