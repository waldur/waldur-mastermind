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
        exclude = ('id',)

    def validate_provider(self, provider):
        if provider not in models.ProviderChoices.CHOICES:
            raise ValidationError('Invalid provider.')
        return provider

    def validate(self, attrs):
        provider = self.instance and self.instance.provider or attrs['provider']
        discovery_url = attrs['discovery_url']
        parsed_url = urlparse(discovery_url)
        hostname = parsed_url.hostname
        if provider == models.ProviderChoices.TARA:
            if hostname not in ('tara-test.ria.ee', 'tara.ria.ee'):
                raise ValidationError('Invalid discovery URL.')
        if provider == models.ProviderChoices.EDUTEAMS:
            if not hostname.endswith('eduteams.org'):
                raise ValidationError('Invalid discovery URL.')
        if provider == models.ProviderChoices.KEYCLOAK:
            if hostname.endswith('eduteams.org') or hostname in (
                'tara-test.ria.ee',
                'tara.ria.ee',
            ):
                raise ValidationError('Invalid discovery URL.')
        return attrs

    def get_fields(self):
        fields = super().get_fields()

        try:
            request = self.context['view'].request
            user = request.user
        except (KeyError, AttributeError):
            return fields

        fields['userinfo_url'].read_only = True
        fields['token_url'].read_only = True
        fields['auth_url'].read_only = True

        if self.instance:
            fields['provider'].read_only = True

        if not user.is_staff:
            del fields['client_secret']

        return fields

    def discover_urls(self, discovery_url):
        try:
            response = requests.get(discovery_url)
        except requests.exceptions.RequestException:
            raise ValidationError('Unable to discover endpoints.')

        try:
            endpoints = response.json()
        except (ValueError, TypeError):
            raise ValidationError('Unable to parse JSON in discovery response.')

        return {
            'userinfo_url': endpoints['userinfo_endpoint'],
            'token_url': endpoints['token_endpoint'],
            'auth_url': endpoints['authorization_endpoint'],
        }

    def update(self, instance, validated_data):
        if instance.discovery_url != validated_data['discovery_url']:
            validated_data |= self.discover_urls(validated_data['discovery_url'])
        return super().update(instance, validated_data)

    def create(self, validated_data):
        if models.IdentityProvider.objects.filter(
            provider=validated_data['provider']
        ).exists():
            raise ValidationError('Identity provider already exists.')

        validated_data |= self.discover_urls(validated_data['discovery_url'])
        return super().create(validated_data)
