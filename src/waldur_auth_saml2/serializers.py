from rest_framework import serializers

from waldur_core.core.serializers import Base64Field
from . import models, utils


class Saml2LoginSerializer(serializers.Serializer):
    idp = serializers.CharField()

    def clean_idp(self, value):
        if utils.is_valid_idp(value):
            return value
        else:
            raise serializers.ValidationError('Identity provider %s is not available.' % value)


class Saml2LoginCompleteSerializer(serializers.Serializer):
    SAMLResponse = Base64Field()


class Saml2LogoutCompleteSerializer(serializers.Serializer):
    SAMLResponse = Base64Field(required=False)
    SAMLRequest = Base64Field(required=False)

    def validate(self, attrs):
        if not attrs.get('SAMLResponse') and not attrs.get('SAMLRequest'):
            raise serializers.ValidationError('Either SAMLResponse or SAMLRequest must be provided.')

        return attrs


class Saml2ProviderSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.IdentityProvider
        fields = ('name', 'url')
