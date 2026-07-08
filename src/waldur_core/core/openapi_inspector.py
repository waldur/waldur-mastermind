from typing import Any

from drf_spectacular.drainage import get_override
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.plumbing import (
    ComponentRegistry,
    OpenApiTypes,
    build_array_type,
    build_basic_type,
    get_doc,
)
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse
from rest_framework.serializers import BaseSerializer, ListSerializer

from waldur_core.core.serializers import RestrictedSerializerMixin
from waldur_core.core.signals import pre_serializer_fields


def _resolve_response_serializer(response):
    """Coerce a drf-spectacular response declaration into a serializer instance.

    ``@extend_schema(responses=...)`` accepts several shapes and stores the
    value verbatim — ``get_response_serializers()`` then returns it as-is.
    Without normalisation we must instance-check against every variant, which
    is fragile (and was missed for the dict case, silently dropping the
    ``?field=`` projection from the schema for every endpoint that uses
    ``responses={status: SerializerClass}``).
    """
    if response is None:
        return None
    if isinstance(response, OpenApiResponse):
        return _resolve_response_serializer(response.response)
    if isinstance(response, dict):
        # Prefer a 2xx entry; fall back to whatever resolves first.
        def _is_success(code):
            try:
                return 200 <= int(code) < 300
            except (TypeError, ValueError):
                return False

        ordered = sorted(
            response.items(), key=lambda item: 0 if _is_success(item[0]) else 1
        )
        for _code, value in ordered:
            resolved = _resolve_response_serializer(value)
            if resolved is not None:
                return resolved
        return None
    if isinstance(response, ListSerializer):
        return response.child
    if isinstance(response, BaseSerializer):
        return response
    if isinstance(response, type) and issubclass(response, BaseSerializer):
        try:
            return response()
        except Exception:
            return None
    return None


class WaldurOpenApiInspector(AutoSchema):
    method_mapping = {
        **AutoSchema.method_mapping,
        "head": "count",
    }

    def get_operation(
        self,
        path: str,
        path_regex: str,
        path_prefix: str,
        method: str,
        registry: ComponentRegistry,
    ) -> dict[str, Any] | None:
        operation = super().get_operation(
            path, path_regex, path_prefix, method, registry
        )
        # Emit HEAD (`_count`) operations for collection endpoints only.
        # Detail views (a single-object retrieve, or a detail-scoped custom
        # action) do not get one by default, since a count is usually
        # meaningless there. A detail action that returns a list can opt in
        # with @count_action (e.g. `/projects/{uuid}/list_users/`).
        if method == "HEAD":
            if getattr(self.view, "detail", False) and not self._count_action_enabled():
                return None
            else:
                operation["responses"] = {"200": {"description": "No response body"}}
                operation["description"] = (
                    "Get number of items in the collection matching the request parameters."
                )
                # An explicit @extend_schema(operation_id=...) on the GET action
                # leaks onto this auto-added HEAD companion (unless it is scoped
                # to methods=["GET"]) and would collide with the GET. Give the
                # HEAD a distinct `_count` id: swap a trailing `_list`, otherwise
                # append `_count`. Ids that are *explicitly* HEAD-specific — a
                # `@extend_schema(methods=["HEAD"], operation_id="..._head")`
                # existence check, e.g. openportal's `retrieve_head` — are
                # intentional and left untouched.
                op_id = operation.get("operationId", "")
                if op_id.endswith("_list"):
                    operation["operationId"] = op_id[: -len("_list")] + "_count"
                elif not op_id.endswith(("_count", "_head")):
                    operation["operationId"] = op_id + "_count"

        if not hasattr(self.view, "action"):
            return operation
        permission_checks = getattr(self.view, self.view.action + "_permissions", [])
        if not isinstance(permission_checks, list):
            return operation

        permissions_data = []
        for check in permission_checks:
            if hasattr(check, "permission"):
                permissions_data.append(
                    {
                        "permission": check.permission.value,
                        "scopes": check.sources,
                    }
                )

        if permissions_data:
            operation["x-permissions"] = permissions_data

        return operation

    def _count_action_enabled(self) -> bool:
        """Whether the current detail action opted into a HEAD `count` variant."""
        action_name = getattr(self.view, "action", None)
        if not action_name:
            return False
        action = getattr(self.view, action_name, None)
        return bool(getattr(action, "count_enabled", False))

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

        serializer = _resolve_response_serializer(self.get_response_serializers())

        if not isinstance(serializer, RestrictedSerializerMixin):
            return []

        # Get the serializer class, not the instance
        serializer_class = serializer.__class__

        try:
            # Create a temporary serializer with schema generation context
            # but also trigger signal-based field additions
            temp_serializer = serializer_class(context={"swagger_fake_view": True})

            # Get fields after initialization to include any dynamically added fields
            fields = list(temp_serializer.get_fields().keys())

            # Additionally, trigger pre_serializer_fields signal to ensure
            # signal-based fields are included in schema generation
            # Create a temporary fields dict to collect signal-based additions
            signal_fields = {}
            pre_serializer_fields.send(
                sender=serializer_class,
                fields=signal_fields,
            )

            # Merge signal-based fields with existing fields
            all_fields = set(fields) | set(signal_fields.keys())
            fields = list(all_fields)

        except Exception:
            # Fallback: try to get fields from the Meta class directly
            try:
                if hasattr(serializer_class, "Meta") and hasattr(
                    serializer_class.Meta, "fields"
                ):
                    meta_fields = serializer_class.Meta.fields
                    if meta_fields == "__all__":
                        # Can't determine fields for __all__, skip parameter generation
                        return []
                    fields = list(meta_fields)

                    # Even in fallback, try to get signal-based fields
                    try:
                        signal_fields = {}
                        pre_serializer_fields.send(
                            sender=serializer_class,
                            fields=signal_fields,
                        )
                        fields = list(set(fields) | set(signal_fields.keys()))
                    except Exception:
                        pass  # If signal processing fails, continue with Meta fields only
                else:
                    return []
            except (KeyError, AttributeError):
                return []

        if not fields or len(fields) == 1:
            return []

        return [
            OpenApiParameter(
                name=RestrictedSerializerMixin.FIELDS_PARAM_NAME,
                type=build_array_type(build_basic_type(OpenApiTypes.STR)),
                location=OpenApiParameter.QUERY,
                enum=sorted(set(fields)),
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
