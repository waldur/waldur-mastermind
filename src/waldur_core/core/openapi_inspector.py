from drf_spectacular.drainage import get_override
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.plumbing import (
    OpenApiTypes,
    build_array_type,
    build_basic_type,
    get_doc,
)
from drf_spectacular.utils import OpenApiParameter
from rest_framework.serializers import ListSerializer

from waldur_core.core.serializers import RestrictedSerializerMixin


class WaldurOpenApiInspector(AutoSchema):
    def get_description(self) -> str:
        action_or_method = getattr(
            self.view, getattr(self.view, "action", self.method.lower()), None
        )
        return get_doc(action_or_method)

    def get_operation_id(self) -> str:
        path = self._tokenize_path()

        use_short = (
            getattr(self.view, "detail", False)
            or getattr(self.view, "action", "") != "create"
        ) and self.method == "POST"

        if not use_short:
            if self.method == "GET" and self._is_list_view():
                path.append("list")
            else:
                path.append(self.method_mapping[self.method.lower()])

        return "_".join([t.replace("-", "_") for t in path])

    def get_override_parameters(self):
        if self.method != "GET":
            return []
        serializer = self.get_response_serializers()
        if not isinstance(serializer, RestrictedSerializerMixin):
            return []
        if isinstance(serializer, ListSerializer):
            serializer = serializer.child
        try:
            fields = serializer.fields.keys()
        except (KeyError, AttributeError):
            return []
        if not fields or len(fields) == 1:
            return []
        return [
            OpenApiParameter(
                name=RestrictedSerializerMixin.FIELDS_PARAM_NAME,
                type=build_array_type(build_basic_type(OpenApiTypes.STR)),
                location=OpenApiParameter.QUERY,
                enum=sorted(fields),
            )
        ]

    def _postprocess_serializer_schema(self, schema, serializer, direction):
        schema = super()._postprocess_serializer_schema(schema, serializer, direction)
        required = schema.get("required", [])
        optional_fields = get_override(serializer, "optional_fields", [])
        if optional_fields:
            schema["required"] = [
                field for field in required if field not in optional_fields
            ]
        return schema
