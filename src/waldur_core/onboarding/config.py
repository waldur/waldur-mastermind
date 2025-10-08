"""
Configuration validation and utilities for the onboarding app.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def validate_onboarding_configuration():
    """
    Validate that all required onboarding configuration is present.

    Raises:
        ImproperlyConfigured: If required configuration is missing or invalid
    """
    waldur_onboarding = getattr(settings, "WALDUR_ONBOARDING", {})

    # Check Estonian Äriregister configuration
    ariregister_config = waldur_onboarding.get("ARIREGISTER", {})

    if not ariregister_config.get("BASE_URL"):
        raise ImproperlyConfigured(
            "WALDUR_ONBOARDING['ARIREGISTER']['BASE_URL'] is required"
        )

    timeout = ariregister_config.get("TIMEOUT", 30)
    if not isinstance(timeout, int) or timeout <= 0:
        raise ImproperlyConfigured(
            "WALDUR_ONBOARDING['ARIREGISTER']['TIMEOUT'] must be a positive integer"
        )


def get_ariregister_config():
    """Get Äriregister configuration with defaults."""
    waldur_onboarding = getattr(settings, "WALDUR_ONBOARDING", {})
    ariregister_config = waldur_onboarding.get("ARIREGISTER", {})

    return {
        "BASE_URL": ariregister_config.get(
            "BASE_URL", "https://avaandmed.ariregister.rik.ee/api"
        ),
        "TIMEOUT": ariregister_config.get("TIMEOUT", 30),
    }
