from django.db import models

GENDER_CHOICES = [
    ("male", "Male"),
    ("female", "Female"),
    ("unknown", "Unknown"),
]


class CoreStates(models.IntegerChoices):
    CREATION_SCHEDULED = 5, "CREATION_SCHEDULED"
    CREATING = 6, "CREATING"
    UPDATE_SCHEDULED = 1, "UPDATE_SCHEDULED"
    UPDATING = 2, "UPDATING"
    DELETION_SCHEDULED = 7, "DELETION_SCHEDULED"
    DELETING = 8, "DELETING"
    OK = 3, "OK"
    ERRED = 4, "ERRED"


class ReviewStates:
    DRAFT = 1
    PENDING = 2
    APPROVED = 3
    REJECTED = 4
    CANCELED = 5

    CHOICES = (
        (DRAFT, "draft"),
        (PENDING, "pending"),
        (APPROVED, "approved"),
        (REJECTED, "rejected"),
        (CANCELED, "canceled"),
    )


class ReauthenticationAction(models.TextChoices):
    """Account changes that need fresh proof of who is making them.

    Holding the API token is not that proof: every sign-in returns the same
    key, so a stolen copy is indistinguishable from the user's own.
    """

    TOKEN_LIFETIME = "token_lifetime", "Change the API token lifetime"
    CHANGE_EMAIL = "change_email", "Change the email address"
    REGISTER_PASSKEY = "register_passkey", "Register a passkey"
    REVOKE_PASSKEY = "revoke_passkey", "Revoke a passkey"
    CREATE_PERSONAL_ACCESS_TOKEN = (
        "create_personal_access_token",
        "Create or rotate a personal access token",
    )
    CHANGE_PASSWORD = "change_password", "Set a user's password"
    WIDEN_PERSONAL_ACCESS_TOKEN_NETWORKS = (
        "widen_personal_access_token_networks",
        "Widen the networks a personal access token works from",
    )


class ReauthenticationMethod(models.TextChoices):
    """How a user proves again that they can sign in."""

    PASSWORD = "password", "Password"
    PASSKEY = "passkey", "Passkey"


class LoginFailureCode(models.TextChoices):
    """Why a SAML2 or OIDC sign-in failed, as the login-failed page is told.

    The backend redirects a failed browser sign-in to the frontend's
    ``login_failed/?code=<code>``. The code is the only thing the URL carries:
    the page shows its own text for each code, so nobody can make the portal
    show text of their choosing by crafting a link. Detail from the identity
    provider or the validation that failed goes to the failed-login event and
    the logs instead.

    An unknown or missing code must be shown as a generic failure. Codes are
    only ever added, never renamed.

    - ``saml2_denied``: the SAML2 identity provider denied the request.
    - ``saml2_invalid_response``: the SAML2 response could not be parsed or
      verified.
    - ``saml2_empty_response``: the SAML2 response could not be matched to a
      request (it parsed to nothing).
    - ``saml2_no_assertion``: the SAML2 response carried no assertion.
    - ``saml2_rejected``: the SAML2 attributes failed validation while the user
      was being signed in or created.
    - ``saml2_failed``: SAML2 authentication found or created no user.
    - ``oidc_invalid_state``: the OIDC sign-in expired or was already used.
    - ``oidc_blocked``: the user may not sign in through OIDC; the page shows
      the configured ``OIDC_BLOCKED_LOGIN_RESPONSE_MESSAGE``.
    - ``oidc_uninvited``: the user has no account and may not get one without
      an invitation; the page shows the configured
      ``OIDC_BLOCK_CREATION_OF_UNINVITED_USERS_RESPONSE_MESSAGE``.
    - ``oidc_email_missing``: account creation needs an email, and the
      identity provider sent none.
    - ``oidc_email_not_trusted``: the email the identity provider sent belongs
      to accounts whose users set it themselves, so it cannot sign them in.
    """

    SAML2_DENIED = "saml2_denied"
    SAML2_INVALID_RESPONSE = "saml2_invalid_response"
    SAML2_EMPTY_RESPONSE = "saml2_empty_response"
    SAML2_NO_ASSERTION = "saml2_no_assertion"
    SAML2_REJECTED = "saml2_rejected"
    SAML2_FAILED = "saml2_failed"
    OIDC_INVALID_STATE = "oidc_invalid_state"
    OIDC_BLOCKED = "oidc_blocked"
    OIDC_UNINVITED = "oidc_uninvited"
    OIDC_EMAIL_MISSING = "oidc_email_missing"
    OIDC_EMAIL_NOT_TRUSTED = "oidc_email_not_trusted"
