from django.conf import settings
from django.db import models


class OAuthToken(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, related_name="auth_profile", on_delete=models.CASCADE
    )
    provider = models.CharField(max_length=32)
    access_token = models.TextField()
    refresh_token = models.TextField()

    class Meta:
        unique_together = ("user", "provider")


class ProviderChoices:
    TARA = "tara"
    EDUTEAMS = "eduteams"
    KEYCLOAK = "keycloak"

    CHOICES = (TARA, EDUTEAMS, KEYCLOAK)


class IdentityProvider(models.Model):
    provider = models.CharField(max_length=32, unique=True)
    is_active = models.BooleanField(default=True)

    client_id = models.CharField(
        help_text="ID of application used for OAuth authentication.", max_length=200
    )
    client_secret = models.CharField(
        help_text="Application secret key.", max_length=200
    )
    verify_ssl = models.BooleanField(default=True)

    # The following fields are cache of URL discovered.
    # They are stored locally in the database in order to avoid extra HTTP request.
    discovery_url = models.CharField(
        max_length=200, help_text="The endpoint for endpoint discovery."
    )
    userinfo_url = models.CharField(
        max_length=200, help_text="The endpoint for fetching user info."
    )
    token_url = models.CharField(
        max_length=200, help_text="The endpoint for obtaining auth token."
    )
    auth_url = models.CharField(
        max_length=200, help_text="The endpoint for authorization request flow."
    )

    label = models.CharField(
        help_text="Human-readable identity provider is label.", max_length=200
    )
    management_url = models.CharField(
        max_length=200,
        help_text="The endpoint for user details management.",
        blank=True,
    )
    protected_fields = models.JSONField(default=list)
