from drf_spectacular.authentication import SessionScheme, TokenScheme
from drf_spectacular.extensions import (
    OpenApiAuthenticationExtension,
    OpenApiSerializerExtension,
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


class OpenStackNestedSecurityGroupSerializerExtension(OpenApiSerializerExtension):
    target_class = "waldur_openstack.serializers.OpenStackNestedSecurityGroupSerializer"

    def map_serializer(self, auto_schema, direction):
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "name": {"type": "string", "readOnly": True},
                "description": {"type": "string", "readOnly": True},
                "state": {"type": "string", "readOnly": True},
                "rules": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "protocol": {"type": "string", "nullable": True},
                            "from_port": {"type": "integer", "nullable": True},
                            "to_port": {"type": "integer", "nullable": True},
                            "cidr": {"type": "string", "nullable": True},
                            "remote_group": {
                                "type": "string",
                                "format": "uri",
                                "nullable": True,
                            },
                            "direction": {"type": "string"},
                            "ethertype": {"type": "string"},
                            "description": {"type": "string", "nullable": True},
                        },
                    },
                    "readOnly": True,
                },
            },
        }
