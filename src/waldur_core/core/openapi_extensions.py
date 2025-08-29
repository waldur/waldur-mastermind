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
