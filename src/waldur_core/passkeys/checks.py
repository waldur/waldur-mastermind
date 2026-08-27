"""Startup validation for the passkey configuration.

A WebAuthn deployment fails in ways that are invisible until a user tries to
sign in: a wrong RP ID means the browser silently offers no credential, and an
origin the RP ID does not cover means every ceremony is rejected. These checks
turn all of that into a startup error instead of a dead button.

Every check is a no-op unless an operator has actually enabled passkeys, so a
deployment that never opts in is unaffected.
"""

from urllib.parse import urlsplit

from django.core.checks import Error, Warning, register

from waldur_core.passkeys import policy

# Hosts for which WebAuthn permits a plain-HTTP origin. Browsers treat these as
# secure contexts, which is what makes local development possible at all.
LOCALHOST_NAMES = ("localhost", "127.0.0.1", "::1")


def _is_localhost(hostname):
    return hostname in LOCALHOST_NAMES


@register()
def passkey_settings_are_valid(app_configs, **kwargs):
    if not policy.is_enabled():
        return []

    errors = []
    rp_id = policy.get_rp_id()
    origins = policy.get_allowed_origins()

    if not rp_id:
        errors.append(
            Error(
                "WALDUR_CORE['PASSKEY_RP_ID'] is required when PASSKEY_SIGNIN or "
                "PASSKEY_MFA is enabled.",
                hint="Set it to the bare hostname the portal is served from, "
                "without scheme or port, e.g. 'waldur.example.com'. It cannot be "
                "derived from the request and changing it later orphans every "
                "registered credential.",
                id="waldur.passkeys.E001",
            )
        )
    elif "://" in rp_id or "/" in rp_id or ":" in rp_id:
        errors.append(
            Error(
                f"WALDUR_CORE['PASSKEY_RP_ID'] must be a bare hostname, got {rp_id!r}.",
                hint="Drop the scheme, port and path: 'https://waldur.example.com/' "
                "should be 'waldur.example.com'.",
                id="waldur.passkeys.E002",
            )
        )

    if not origins:
        errors.append(
            Error(
                "WALDUR_CORE['PASSKEY_ALLOWED_ORIGINS'] is required when "
                "PASSKEY_SIGNIN or PASSKEY_MFA is enabled.",
                hint="List the full origins the portal is served from, "
                "e.g. ['https://waldur.example.com'].",
                id="waldur.passkeys.E003",
            )
        )

    for origin in origins:
        parts = urlsplit(origin)
        if not parts.scheme or not parts.hostname:
            errors.append(
                Error(
                    f"{origin!r} in WALDUR_CORE['PASSKEY_ALLOWED_ORIGINS'] is not a "
                    "full origin.",
                    hint="Include the scheme, e.g. 'https://waldur.example.com'.",
                    id="waldur.passkeys.E004",
                )
            )
            continue

        if parts.scheme != "https" and not _is_localhost(parts.hostname):
            errors.append(
                Error(
                    f"{origin!r} in WALDUR_CORE['PASSKEY_ALLOWED_ORIGINS'] is not "
                    "HTTPS.",
                    hint="Browsers only expose WebAuthn in a secure context. Plain "
                    "HTTP is permitted for localhost only.",
                    id="waldur.passkeys.E005",
                )
            )

        # The RP ID must be the origin's host or a registrable suffix of it,
        # otherwise the browser rejects every ceremony from that origin.
        if rp_id and parts.hostname != rp_id:
            if not parts.hostname.endswith("." + rp_id):
                errors.append(
                    Error(
                        f"{origin!r} in WALDUR_CORE['PASSKEY_ALLOWED_ORIGINS'] is not "
                        f"subordinate to the RP ID {rp_id!r}.",
                        hint="An origin may only run ceremonies for an RP ID that is "
                        "its own host or a parent domain of it.",
                        id="waldur.passkeys.E006",
                    )
                )

    return errors


@register()
def passkey_credentials_are_not_orphaned(app_configs, **kwargs):
    """Warn when the RP ID no longer matches credentials already registered.

    Changing the RP ID does not fail anything loudly — it just means every
    existing credential stops being offered by the browser. Counting them at
    startup is the only cheap way to notice.
    """
    if not policy.is_enabled():
        return []

    rp_id = policy.get_rp_id()
    if not rp_id:
        # Already reported as E001; counting against an empty RP ID would flag
        # every credential in the database for no reason.
        return []

    try:
        from waldur_core.passkeys.models import PasskeyCredential

        orphaned = (
            PasskeyCredential.objects.filter(is_active=True)
            .exclude(rp_id=rp_id)
            .count()
        )
    except Exception:
        # Checks run before migrations on a fresh database, and during
        # collectstatic in image builds. An unavailable table is not a
        # configuration problem.
        return []

    if not orphaned:
        return []

    return [
        Warning(
            f"{orphaned} active passkey credential(s) were registered under a "
            f"different RP ID and can no longer be used to sign in.",
            hint=f"The configured RP ID is {rp_id!r}. Credentials are bound to the "
            "RP ID they were registered under; affected users must enrol again.",
            id="waldur.passkeys.W001",
        )
    ]
