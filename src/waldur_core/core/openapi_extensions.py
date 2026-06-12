from drf_spectacular.authentication import SessionScheme, TokenScheme
from drf_spectacular.extensions import (
    OpenApiAuthenticationExtension,
    OpenApiSerializerFieldExtension,
)
from drf_spectacular.plumbing import (
    build_basic_type,
    build_bearer_security_scheme_object,
)
from drf_spectacular.types import OpenApiTypes


class WaldurTokenScheme(TokenScheme):
    target_class = "waldur_core.core.authentication.TokenAuthentication"
    name = "waldurTokenAuth"


class WaldurSessionScheme(SessionScheme):
    target_class = "waldur_core.core.authentication.SessionAuthentication"
    name = "waldurCookieAuth"


class PATAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "waldur_core.core.authentication.PATAuthentication"
    name = "waldurPATAuth"

    def get_security_definition(self, auto_schema):
        return build_bearer_security_scheme_object(
            header_name="Authorization",
            token_prefix="Bearer",
            bearer_format="w_<unix_timestamp>_<random>",
        )


class OIDCAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "waldur_core.core.authentication.OIDCAuthentication"
    name = "waldurOIDCAuth"

    def get_security_definition(self, auto_schema):
        return build_bearer_security_scheme_object(
            header_name="Authorization",
            token_prefix="Bearer",
        )


class GenericRelatedFieldExtension(OpenApiSerializerFieldExtension):
    target_class = "waldur_core.core.serializers.GenericRelatedField"

    def map_serializer_field(self, auto_schema, direction):
        return build_basic_type(OpenApiTypes.STR)


IP_ADDRESS_BOTH_SCHEMA = {
    "description": "An IPv4 or IPv6 address.",
    "oneOf": [
        {"type": "string", "format": "ipv4"},
        {"type": "string", "format": "ipv6"},
    ],
}


class IPAddressFieldExtension(OpenApiSerializerFieldExtension):
    # Target the specific DRF field class we want to customize
    target_class = "rest_framework.fields.IPAddressField"

    def map_serializer_field(self, auto_schema, direction):
        # self.target is the actual instance of the field on the serializer
        field = self.target

        # Only apply our custom schema if the protocol is 'both' (the default)
        if field.protocol == "both":
            return IP_ADDRESS_BOTH_SCHEMA

        if field.protocol == "ipv4":
            return {"type": "string", "format": "ipv4"}

        if field.protocol == "ipv6":
            return {"type": "string", "format": "ipv6"}


class JSONFieldExtension(OpenApiSerializerFieldExtension):
    target_class = "rest_framework.fields.JSONField"

    def map_serializer_field(self, auto_schema, direction):
        return {"type": "object", "additionalProperties": True}


from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


@extend_schema_field(OpenApiTypes.ANY)
class AnyJSONField(serializers.Field):
    def to_representation(self, value):
        return value

    def to_internal_value(self, data):
        return data
