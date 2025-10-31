"""
Abstract base classes for company registry backends.

This module provides the foundation for an extensible backend system that
can be extended for additional countries and validation methods in the future.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from waldur_core.core.models import User


@dataclass
class ValidationRequest:
    """
    Standardized request object for company validation.

    Contains all the information needed to validate a user's authorization
    to represent a company.
    """

    country: str
    person_identifier: str | dict  # Personal ID

    legal_person_identifier: str | None = None  # Company registration code
    legal_name: str | None = None  # Company name

    # Additional context for future extensibility
    additional_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    """Standardized response from company validation."""

    is_valid: bool
    method_used: str  # Name of the validation method used

    # Company information retrieved
    company_data: dict[str, Any]

    # Authorization details
    user_roles: list[str]  # Roles the user has in the company

    # Raw response for debugging/auditing
    raw_response: dict[str, Any]

    # Error details if validation failed
    error_code: str | None = None
    error_message: str | None = None


class CompanyRegistryBackend(ABC):
    """
    Abstract base class for company registry backends.

    Each backend handles validation for specific countries and methods.
    The design allows for easy extension to new countries and methods.
    """

    @classmethod
    @abstractmethod
    def get_supported_countries(cls) -> set[str]:
        """Return set of ISO country codes this backend supports."""
        pass

    @classmethod
    @abstractmethod
    def get_validation_method(cls) -> str:
        """Return the name of the validation method this backend implements."""
        pass

    @classmethod
    @abstractmethod
    def get_required_fields(cls) -> list[str]:
        """Return list of required fields in ValidationRequest for this backend."""
        pass

    @classmethod
    def get_priority(cls) -> int:
        """Return priority level (lower = higher priority). Default is 100."""
        return 100

    @abstractmethod
    def validate_company(self, request: ValidationRequest) -> ValidationResult:
        """
        Validate company authorization.

        Args:
            request: ValidationRequest with all necessary parameters

        Returns:
            ValidationResult with validation outcome
        """
        pass

    @abstractmethod
    def validate_user_identity(self, user) -> tuple[bool, str]:
        """
        Validate user has required identity information for this backend.

        Args:
            user: User object to validate

        Returns:
            Tuple of (is_valid, error_message)
            - is_valid: True if user has required identity information
            - error_message: Empty string if valid, error description if not valid
        """
        pass

    def can_handle_request(self, request: ValidationRequest) -> bool:
        """
        Check if this backend can handle the given request.

        Validates:
        1. Country is supported
        2. Required parameters are present
        """
        # Check country support
        if request.country not in self.get_supported_countries():
            return False

        # Check required fields
        for field_name in self.get_required_fields():
            if not getattr(request, field_name, None):
                return False

        return True

    @staticmethod
    def get_person_identifier_from_user(user: User) -> str | dict:
        pass


class BackendRegistry:
    """
    Registry for managing validation backends.

    Handles backend discovery, selection, and provides a unified interface
    for validation requests.
    """

    def __init__(self):
        self._backends: list[type[CompanyRegistryBackend]] = []

    def register_backend(self, backend_class: type[CompanyRegistryBackend]):
        """Register a new backend class."""
        if backend_class not in self._backends:
            self._backends.append(backend_class)

    def get_available_backends_for_country(
        self, country: str
    ) -> list[type[CompanyRegistryBackend]]:
        """Get all backends that support the given country, ordered by priority."""
        backends = []

        for backend_class in self._backends:
            if country in backend_class.get_supported_countries():
                backends.append((backend_class, backend_class.get_priority()))

        # Sort by priority (lower number = higher priority)
        backends.sort(key=lambda x: x[1])
        return [backend for backend, _ in backends]

    def find_backend_for_request(
        self, request: ValidationRequest
    ) -> CompanyRegistryBackend | None:
        """Find the best backend that can handle the given request."""
        available_backends = self.get_available_backends_for_country(request.country)

        for backend_class in available_backends:
            backend_instance = backend_class()
            if backend_instance.can_handle_request(request):
                return backend_instance

        return None

    def validate_company(self, request: ValidationRequest) -> ValidationResult:
        """
        Validate company using the best available backend.

        Args:
            request: ValidationRequest with company and user information

        Returns:
            ValidationResult with validation outcome
        """
        backend = self.find_backend_for_request(request)

        if not backend:
            return ValidationResult(
                is_valid=False,
                method_used="none",
                company_data={},
                user_roles=[],
                raw_response={},
                error_code="NO_BACKEND_AVAILABLE",
                error_message=f"No validation backend available for country {request.country}",
            )

        try:
            return backend.validate_company(request)
        except ValueError:
            # Let ValueError (configuration errors) bubble up to be handled by validator
            raise
        except Exception as e:
            return ValidationResult(
                is_valid=False,
                method_used=backend.get_validation_method(),
                company_data={},
                user_roles=[],
                raw_response={},
                error_code="BACKEND_ERROR",
                error_message=f"Backend error: {str(e)}",
            )


# Global registry instance
backend_registry = BackendRegistry()
