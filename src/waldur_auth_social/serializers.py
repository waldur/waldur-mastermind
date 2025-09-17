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
