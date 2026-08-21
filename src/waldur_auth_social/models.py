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
    enable_post_logout_redirect = models.BooleanField(default=True)
    enable_pkce = models.BooleanField(default=False)

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
    logout_url = models.CharField(
        max_length=200,
        help_text="The endpoint used to redirect after sign-out.",
        default="",
        blank=True,
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
    extra_scope = models.CharField(
        max_length=200,
        help_text="Space-separated list of scopes to request during authentication.",
        null=True,
        blank=True,
    )
    user_field = models.CharField(
        max_length=100,
        default="username",
        help_text="The field in Waldur User model to be used for looking up the user",
    )
    user_claim = models.CharField(
        max_length=100,
        default="sub",
        help_text="The OIDC claim from the userinfo endpoint to be "
        "used as the value for the lookup field.",
    )
    attribute_mapping = models.JSONField(
        default=dict,
        blank=True,
        help_text="A JSON object mapping Waldur User model fields to OIDC claims. "
        'Example: {"first_name": "given_name", "last_name": "family_name", "email": "email"}',
    )
    extra_fields = models.CharField(
        max_length=200,
        help_text="Space-separated list of extra fields to persist.",
        null=True,
        blank=True,
    )
    allowed_redirects = models.JSONField(
        default=list,
        blank=True,
        help_text="List of allowed redirect URLs for OAuth authentication. "
        "URLs must be exact matches (origin only: scheme + domain + port). "
        "HTTPS required except for localhost. No wildcards, paths, query params, or fragments. "
        'Example: ["https://portal1.example.com", "https://portal2.example.com:8443"]. '
        "If empty, falls back to HOMEPORT_URL setting.",
    )

    class Meta:
        ordering = ["label", "id"]

    def __str__(self):
        return f"{self.label}: {self.provider} / {self.is_active}"
