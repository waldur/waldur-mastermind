from urllib.parse import urlparse

import requests
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from waldur_auth_social.const import (
    PROVIDER_DEFAULTS,
    SECRET_PROVIDER_FIELDS,
    WRITABLE_USER_FIELDS,
    ProviderChoices,
)

from . import models


class AuthSerializer(serializers.Serializer):
    client_id = serializers.CharField()
    redirect_uri = serializers.CharField()
    code = serializers.CharField()


class RemoteEduteamsRequestSerializer(serializers.Serializer):
    cuid = serializers.CharField(max_length=256)


class IdentityProviderSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.IdentityProvider
        exclude = ("id",)
        extra_kwargs = {
            "userinfo_url": {"read_only": True},
            "token_url": {"read_only": True},
            "auth_url": {"read_only": True},
            "logout_url": {"read_only": True},
        }

    def validate_provider(self, provider):
        if provider not in ProviderChoices.CHOICES:
            raise ValidationError("Invalid provider.")
        return provider

    def validate(self, attrs):
        provider = self.instance and self.instance.provider or attrs["provider"]
        discovery_url = attrs["discovery_url"]
        parsed_url = urlparse(discovery_url)
        hostname = parsed_url.hostname
        if not hostname or parsed_url.scheme.lower() not in ("http", "https"):
            raise ValidationError("Invalid discovery URL.")
        if provider == ProviderChoices.TARA:
            if hostname not in ("tara-test.ria.ee", "tara.ria.ee"):
                raise ValidationError("Invalid discovery URL.")
        if provider == ProviderChoices.KEYCLOAK:
            if hostname.endswith("eduteams.org") or hostname in (
                "tara-test.ria.ee",
                "tara.ria.ee",
            ):
                raise ValidationError("Invalid discovery URL.")
        return attrs

    def validate_attribute_mapping(self, attrs: dict[str, str]):
        invalid = set(attrs.keys()) - set(WRITABLE_USER_FIELDS)
        if invalid:
            raise ValidationError(
                f"Invalid attribute mapping keys: {','.join(invalid)}"
            )
        for key, value in attrs.items():
            if not isinstance(value, str):
                raise ValidationError(
                    f"Attribute mapping value for '{key}' must be a string."
                )
            if not value.strip():
                raise ValidationError(
                    f"Attribute mapping value for '{key}' is empty string."
                )
        return attrs

    def validate_allowed_redirects(self, allowed_redirects: list[str]):
        if not isinstance(allowed_redirects, list):
            raise ValidationError("allowed_redirects must be a list of URLs.")

        # Use URLField for basic URL validation
        url_field = serializers.URLField()
        validated_urls = []

        for url in allowed_redirects:
            if not isinstance(url, str):
                raise ValidationError("Each redirect URL must be a string.")

            # Validate URL format using DRF's URLField
            try:
                url_field.run_validation(url)
            except ValidationError as e:
                raise ValidationError(f"Invalid redirect URL {url}: {e.detail[0]}")

            # Parse URL for additional validation
            parsed = urlparse(url)

            # Only allow http/https schemes
            if parsed.scheme.lower() not in ("http", "https"):
                raise ValidationError(
                    f"Invalid URL scheme for {url}. Only http and https are allowed."
                )

            # Enforce HTTPS except for localhost
            is_localhost = (
                parsed.netloc.lower() in ("localhost", "127.0.0.1")
                or parsed.netloc.lower().startswith("localhost:")
                or parsed.netloc.lower().startswith("127.0.0.1:")
            )

            if parsed.scheme == "http" and not is_localhost:
                raise ValidationError(
                    f"HTTPS required for {url}. HTTP is only allowed for localhost."
                )

            # Reject URLs with paths (except "/" or empty)
            if parsed.path and parsed.path != "/":
                raise ValidationError(
                    f"Redirect URL must not contain a path: {url}. "
                    f"Only origin (scheme + domain + port) is allowed."
                )

            # Reject query parameters
            if parsed.query:
                raise ValidationError(
                    f"Redirect URL must not contain query parameters: {url}"
                )

            # Reject fragments
            if parsed.fragment:
                raise ValidationError(f"Redirect URL must not contain fragments: {url}")

            # Normalize: lowercase scheme and netloc, no trailing slash for exact matching
            normalized_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            validated_urls.append(normalized_url)

        return validated_urls

    def get_fields(self):
        fields = super().get_fields()

        try:
            request = self.context["request"]
            user = request.user
        except (KeyError, AttributeError):
            return fields

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        if self.instance:
            fields["provider"].read_only = True

        if not user.is_staff:
            for field in SECRET_PROVIDER_FIELDS:
                del fields[field]

        return fields

    def discover_urls(self, discovery_url, verify_ssl=True):
        try:
            response = requests.get(discovery_url, verify=verify_ssl)
            response.raise_for_status()
        except requests.exceptions.RequestException:
            raise ValidationError("Unable to discover endpoints.")

        try:
            endpoints = response.json()
            return {
                "userinfo_url": endpoints["userinfo_endpoint"],
                "token_url": endpoints["token_endpoint"],
                "auth_url": endpoints["authorization_endpoint"],
                "logout_url": endpoints.get("end_session_endpoint") or "",
            }
        except (requests.JSONDecodeError, KeyError, TypeError):
            raise ValidationError("Unable to parse JSON in discovery response.")

    def update(self, instance, validated_data):
        verify_ssl = validated_data.get("verify_ssl", True)
        validated_data |= self.discover_urls(
            validated_data["discovery_url"], verify_ssl
        )
        protected_fields = validated_data.get("protected_fields")
        if isinstance(protected_fields, str):
            protected_fields = [field.strip() for field in protected_fields.split(",")]
        if protected_fields == [""]:
            protected_fields = []
        if protected_fields is not None:
            validated_data["protected_fields"] = protected_fields
        return super().update(instance, validated_data)

    def create(self, validated_data):
        provider = validated_data["provider"]
        if models.IdentityProvider.objects.filter(provider=provider).exists():
            raise ValidationError("Identity provider already exists.")

        verify_ssl = validated_data.get("verify_ssl", True)
        validated_data |= self.discover_urls(
            validated_data["discovery_url"], verify_ssl
        )
        default_values = PROVIDER_DEFAULTS.get(provider)
        if default_values:
            for key, value in default_values.items():
                if isinstance(value, dict):
                    validated_data.setdefault(key, {})
                    for nested_key, nested_value in value.items():
                        validated_data[key].setdefault(nested_key, nested_value)
                else:
                    validated_data.setdefault(key, value)
        return super().create(validated_data)


class RemoteEduteamsUUIDSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()


class DiscoverMetadataRequestSerializer(serializers.Serializer):
    discovery_url = serializers.URLField(
        help_text="OIDC discovery URL (e.g., https://idp.example.com/.well-known/openid-configuration)"
    )
    verify_ssl = serializers.BooleanField(
        default=True, help_text="Whether to verify SSL certificate"
    )


class WaldurFieldSuggestionSerializer(serializers.Serializer):
    field = serializers.CharField(help_text="Waldur User model field name")
    description = serializers.CharField(help_text="Human-readable field description")
    suggested_claims = serializers.ListField(
        child=serializers.CharField(),
        help_text="OIDC claims that could map to this field, ordered by likelihood",
    )
    available_claims = serializers.ListField(
        child=serializers.CharField(),
        help_text="Claims from this IdP that match the suggestions",
    )


class DiscoverMetadataResponseSerializer(serializers.Serializer):
    claims_supported = serializers.ListField(
        child=serializers.CharField(),
        help_text="List of claims supported by the OIDC provider",
    )
    scopes_supported = serializers.ListField(
        child=serializers.CharField(),
        help_text="List of scopes supported by the OIDC provider",
    )
    endpoints = serializers.DictField(
        child=serializers.CharField(),
        help_text="OIDC endpoints (authorization, token, userinfo, logout)",
    )
    waldur_fields = WaldurFieldSuggestionSerializer(
        many=True,
        help_text="Waldur User fields with suggested OIDC claim mappings",
    )
    suggested_scopes = serializers.ListField(
        child=serializers.CharField(),
        help_text="Recommended scopes to request based on claim mappings",
    )
