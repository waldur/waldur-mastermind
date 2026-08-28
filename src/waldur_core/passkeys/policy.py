"""Where passkey behaviour is decided.

This module is imported by ``waldur_core.core.views`` in phase 2, which is why
passkeys are a core sub-app rather than a ``WaldurExtension``: core importing
an extension would invert the entry-point contract.

It deliberately contains no I/O and no Django model access, so it stays cheap
to call from the login path.
"""

from django.conf import settings

# Values used in WALDUR_CORE["AUTHENTICATION_METHODS"].
PASSKEY_SIGNIN = "PASSKEY_SIGNIN"
PASSKEY_MFA = "PASSKEY_MFA"


def _methods():
    return settings.WALDUR_CORE.get("AUTHENTICATION_METHODS") or []


def is_signin_enabled() -> bool:
    """Passwordless sign-in with a discoverable credential."""
    return PASSKEY_SIGNIN in _methods()


def is_mfa_enabled() -> bool:
    """Passkey as a second factor after a correct password."""
    return PASSKEY_MFA in _methods()


def is_enabled() -> bool:
    """True when passkeys are usable at all on this deployment.

    Everything in this app is inert unless an operator opts in, so this is the
    single gate the API and the UI both read.
    """
    return is_signin_enabled() or is_mfa_enabled()


def get_rp_id() -> str:
    return settings.WALDUR_CORE.get("PASSKEY_RP_ID") or ""


def get_rp_name() -> str:
    """Human-readable name the authenticator shows during enrolment.

    Falls back to the deployment's site name, as the setting documents. The
    library rejects an empty value outright, so without this an operator who
    leaves it unset — which the helm chart allows, since it only renders the
    key when a value is given — gets ``ValueError: rp_name cannot be an empty
    string`` on the first enrolment attempt, and passkeys cannot be used at
    all.
    """
    configured = settings.WALDUR_CORE.get("PASSKEY_RP_NAME")
    if configured:
        return configured
    # Imported here rather than at module scope: this module is imported from
    # waldur_core.core.authentication, which loads long before Constance is
    # usable, and touching config at import time would break startup.
    from constance import config

    return config.SITE_NAME or "Waldur"


def get_allowed_origins() -> list[str]:
    return list(settings.WALDUR_CORE.get("PASSKEY_ALLOWED_ORIGINS") or [])
