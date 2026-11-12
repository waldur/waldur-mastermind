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
