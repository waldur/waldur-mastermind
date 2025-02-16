from drf_spectacular.authentication import SessionScheme, TokenScheme
from drf_spectacular.extensions import OpenApiSerializerFieldExtension
from drf_spectacular.plumbing import build_basic_type
from drf_spectacular.types import OpenApiTypes


class WaldurTokenScheme(TokenScheme):
    target_class = "waldur_core.core.authentication.TokenAuthentication"
    name = "waldurTokenAuth"


class WaldurSessionScheme(SessionScheme):
    target_class = "waldur_core.core.authentication.SessionAuthentication"
    name = "waldurCookieAuth"


class GenericRelatedFieldExtension(OpenApiSerializerFieldExtension):
    target_class = "waldur_core.core.serializers.GenericRelatedField"

    def map_serializer_field(self, auto_schema, direction):
        return build_basic_type(OpenApiTypes.STR)
