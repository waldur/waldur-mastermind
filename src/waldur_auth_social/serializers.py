from urllib.parse import urlparse

import requests
from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from . import models

User = get_user_model()


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
        }

    def validate_provider(self, provider):
        if provider not in models.ProviderChoices.CHOICES:
            raise ValidationError("Invalid provider.")
        return provider

    def validate(self, attrs):
        provider = self.instance and self.instance.provider or attrs["provider"]
        discovery_url = attrs["discovery_url"]
        parsed_url = urlparse(discovery_url)
        hostname = parsed_url.hostname
        if not hostname or parsed_url.scheme.lower() not in ("http", "https"):
            raise ValidationError("Invalid discovery URL.")
        if provider == models.ProviderChoices.TARA:
            if hostname not in ("tara-test.ria.ee", "tara.ria.ee"):
                raise ValidationError("Invalid discovery URL.")
        if provider == models.ProviderChoices.EDUTEAMS:
            if not hostname.endswith("eduteams.org"):
                raise ValidationError("Invalid discovery URL.")
        if provider == models.ProviderChoices.KEYCLOAK:
            if hostname.endswith("eduteams.org") or hostname in (
                "tara-test.ria.ee",
                "tara.ria.ee",
            ):
                raise ValidationError("Invalid discovery URL.")
        return attrs

    def get_fields(self):
        fields = super().get_fields()

        try:
            request = self.context["view"].request
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if self.instance:
            fields["provider"].read_only = True

        if not user.is_staff:
            del fields["client_secret"]

        return fields

    def discover_urls(self, discovery_url, verify_ssl=True):
        try:
            response = requests.get(discovery_url, verify=verify_ssl)
        except requests.exceptions.RequestException:
            raise ValidationError("Unable to discover endpoints.")

        try:
            endpoints = response.json()
        except (ValueError, TypeError):
            raise ValidationError("Unable to parse JSON in discovery response.")

        return {
            "userinfo_url": endpoints["userinfo_endpoint"],
            "token_url": endpoints["token_endpoint"],
            "auth_url": endpoints["authorization_endpoint"],
        }

    def update(self, instance, validated_data):
        if instance.discovery_url != validated_data["discovery_url"]:
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
        if models.IdentityProvider.objects.filter(
            provider=validated_data["provider"]
        ).exists():
            raise ValidationError("Identity provider already exists.")

        verify_ssl = validated_data.get("verify_ssl", True)
        validated_data |= self.discover_urls(
            validated_data["discovery_url"], verify_ssl
        )
        return super().create(validated_data)
