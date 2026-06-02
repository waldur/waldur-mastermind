import copy
import re
from typing import Any
from unittest import mock

from django.urls import Resolver404, resolve
from drf_spectacular.drainage import error
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.openapi import AutoSchema

from waldur_core.core import filters as core_filters


def postprocess_drop_description(result, generator, **kwargs):
    """
    Remove descriptions from OpenAPI schema components.
    """
    for methods in result.get("components", {}).values():
        for operation in methods.values():
            operation["description"] = ""
    return result


def postprocess_fix_enum(result, generator, **kwargs):
    """
    Replace integer enum type with string type.
    """
    for methods in result["paths"].values():
        for operation in methods.values():
            for parameter in operation.get("parameters", []):
                if (
                    parameter["schema"]["type"] == "array"
                    and parameter["schema"]["items"].get("type") == "integer"
                ):
                    parameter["schema"]["items"]["type"] = "string"
    return result


def refactor_pagination_parameters(result, generator, **kwargs):
    """
    Refactor an OpenAPI schema to extract pagination parameters, add them to
    components/parameters, and replace original definitions with references.
    """
    # Ensure components and parameters sections exist
    if "parameters" not in result["components"]:
        result["components"]["parameters"] = {}

    # Define the pagination parameters we want to extract
    pagination_params = {
        "Page": {
            "name": "page",
            "required": False,
            "in": "query",
            "description": "A page number within the paginated result set.",
            "schema": {"type": "integer"},
        },
        "PageSize": {
            "name": "page_size",
            "required": False,
            "in": "query",
            "description": "Number of results to return per page.",
            "schema": {"type": "integer"},
        },
    }

    # Add pagination parameters to components/parameters
    for param_name, param_def in pagination_params.items():
        result["components"]["parameters"][param_name] = param_def

    # Iterate through paths and operations to replace pagination parameters with references
    for path in result.get("paths", {}).values():
        for operation in [op for op in path.values() if isinstance(op, dict)]:
            if "parameters" in operation:
                refactor_operation_parameters(operation, pagination_params)

    return result


def refactor_operation_parameters(operation, pagination_params):
    """
    Refactor the parameters in an operation by replacing pagination parameters with references.
    """
    new_parameters = []
    pagination_refs_added = set()

    for param in operation["parameters"]:
        # Check if this parameter matches one of our pagination parameters
        is_pagination = False
        for param_name, param_def in pagination_params.items():
            if (
                param.get("name") == param_def["name"]
                and param.get("in") == param_def["in"]
            ):
                # It's a pagination parameter, replace with reference if not already added
                if param_name not in pagination_refs_added:
                    new_parameters.append(
                        {"$ref": f"#/components/parameters/{param_name}"}
                    )
                    pagination_refs_added.add(param_name)
                is_pagination = True
                break

        # If it's not a pagination parameter, keep it
        if not is_pagination:
            new_parameters.append(param)

    operation["parameters"] = new_parameters


def transform_paginated_arrays(result, generator, **kwargs):
    """
    Traverses an OpenAPI schema and transforms all references to components with
    names starting with 'Paginated' by replacing them with the array structure directly.
    """
    # Identify all paginated components
    paginated_components = {}
    other_components = {}
    for name, component in result["components"]["schemas"].items():
        if name.startswith("Paginated") and component.get("type") == "array":
            paginated_components[name] = component
        else:
            other_components[name] = component

    # Function to recursively replace references to paginated components
    def replace_references(obj):
        if isinstance(obj, dict):
            if "$ref" in obj and isinstance(obj["$ref"], str):
                ref_path = obj["$ref"]
                if ref_path.startswith("#/components/schemas/"):
                    component_name = ref_path.split("/")[-1]
                    if component_name in paginated_components:
                        # Replace reference with the array definition
                        paginated_component = paginated_components[component_name]
                        # Keep any additional properties from the original object (except $ref)
                        ref_obj = {k: v for k, v in obj.items() if k != "$ref"}
                        # Merge with the array definition
                        obj.clear()
                        obj.update({**paginated_component, **ref_obj})

            # Continue recursively
            for value in obj.values():
                replace_references(value)
        elif isinstance(obj, list):
            for item in obj:
                replace_references(item)

    # Process the entire schema
    replace_references(result)

    result["components"]["schemas"] = other_components

    return result


def add_result_count_header(result, generator, **kwargs):
    """
    Adds x-result-count header to all paginated endpoints using a reusable component.
    """
    # Define reusable header component
    header_name = "XResultCount"
    header_def = {
        "description": "Total number of results available",
        "schema": {"type": "integer"},
        "example": 42,
    }

    # Ensure components section exists
    components = result.setdefault("components", {})
    headers = components.setdefault("headers", {})

    # Add header definition to components if not exists
    if header_name not in headers:
        headers[header_name] = header_def

    # Process all endpoints
    for path_item in result.get("paths", {}).values():
        for operation in path_item.values():
            if not operation.get("operationId", "").endswith("_list"):
                continue

            responses = operation.get("responses", {})
            if not responses:
                continue

            # Process all 2xx responses
            for status_code, response in responses.items():
                if not status_code.startswith("2"):
                    continue

                # Get content schemas
                content = response.get("content", {})
                if not content:
                    continue

                # Check each media type schema for pagination
                for media_obj in content.values():
                    schema = media_obj.get("schema")
                    if not schema:
                        continue

                    # Add header reference to response
                    if "headers" not in response:
                        response["headers"] = {}
                    response["headers"]["x-result-count"] = {
                        "$ref": f"#/components/headers/{header_name}"
                    }
                    break  # Only need to add once per response
    return result


def make_fields_optional(result, generator, **kwargs):
    """
    Modifies an OpenAPI schema to make all fields optional in responses for
    endpoints that have a "field" query parameter.
    """
    for path in result["paths"].values():
        for operation in path.values():
            has_field_param = any(
                param.get("in") == "query" and param.get("name") == "field"
                for param in operation.get("parameters", [])
            )

            if not has_field_param:
                continue

            for response in operation.get("responses", {}).values():
                for content in response.get("content").values():
                    if "schema" in content:
                        _make_fields_optional(content["schema"], result)

    return result


def _make_fields_optional(schema_obj, full_schema):
    """
    Recursively makes all fields optional in a schema object.
    """
    # Handle schema reference
    if "$ref" in schema_obj:
        ref_path = schema_obj["$ref"]
        if ref_path.startswith("#/components/schemas/"):
            schema_name = ref_path.split("/")[-1]
            if (
                "components" in full_schema
                and "schemas" in full_schema["components"]
                and schema_name in full_schema["components"]["schemas"]
            ):
                referenced_schema = full_schema["components"]["schemas"][schema_name]
                _make_fields_optional(referenced_schema, full_schema)
        return

    # Handle array items
    if schema_obj.get("type") == "array" and "items" in schema_obj:
        _make_fields_optional(schema_obj["items"], full_schema)
        return

    # Handle object properties
    if schema_obj.get("type") == "object" or "properties" in schema_obj:
        if "required" in schema_obj:
            # Clear the required array to make all fields optional
            schema_obj["required"] = []

        # Recursively process properties
        if "properties" in schema_obj:
            for prop_schema in schema_obj["properties"].values():
                _make_fields_optional(prop_schema, full_schema)

    # Handle allOf, oneOf, anyOf
    for key in ["allOf", "oneOf", "anyOf"]:
        if key in schema_obj:
            for sub_schema in schema_obj[key]:
                _make_fields_optional(sub_schema, full_schema)


# Maps a response schema component name to the set of read-only fields that
# its serializer's get_fields() may strip from the response depending on the
# requester's role, ownership, or a runtime setting. drf-spectacular forces
# every read-only field into required[], so without this hook the generated
# SDKs (notably the attrs-based Python client) reject legitimate responses
# that omit those fields.
CONDITIONALLY_OPTIONAL_RESPONSE_FIELDS: dict[str, set[str]] = {
    # UserSerializer / UserMeSerializer (waldur_core.structure.serializers)
    "User": {
        "token",
        "token_lifetime",
        "attribute_sources",
        "active_isds",
        "has_active_session",
        "has_usable_password",
    },
    "UserMe": {
        "token",
        "token_lifetime",
        "attribute_sources",
        "active_isds",
        "has_active_session",
        "has_usable_password",
    },
    # IdentityProviderSerializer (waldur_auth_social.serializers)
    # OAuth credentials dropped for non-staff users.
    "IdentityProvider": {
        "client_id",
        "client_secret",
        "discovery_url",
        "token_url",
        "userinfo_url",
        "auth_url",
        "logout_url",
    },
    # MessageSerializer (waldur_mastermind.chat.serializers)
    # Moderation metadata dropped for non-staff/support users.
    "Message": {
        "is_flagged",
        "severity",
        "injection_categories",
        "pii_categories",
        "action_taken",
    },
    # IssueSerializer (waldur_mastermind.support.serializers)
    # Internal triage links dropped for non-staff/support users.
    "Issue": {"link", "processing_log"},
}


def relax_conditionally_optional_fields(result, generator, **kwargs):
    """
    Strip fields that the serializer's get_fields() may delete at runtime from
    each response component's required[] array, so SDKs accept responses that
    legitimately omit them. See CONDITIONALLY_OPTIONAL_RESPONSE_FIELDS above.
    """
    schemas = result.get("components", {}).get("schemas", {})
    for schema_name, optional_fields in CONDITIONALLY_OPTIONAL_RESPONSE_FIELDS.items():
        schema = schemas.get(schema_name)
        if not schema or "required" not in schema:
            continue
        schema["required"] = [f for f in schema["required"] if f not in optional_fields]
    return result


def remove_waldur_cookie_auth(result, generator, **kwargs):
    """
    Remove waldurCookieAuth from security schemes in an OpenAPI schema.
    """
    for path in result["paths"].values():
        for operation in path.values():
            if "security" in operation:
                operation["security"] = [
                    sec_req
                    for sec_req in operation["security"]
                    if "waldurCookieAuth" not in sec_req
                ]
                # If security becomes empty, remove it
                if not operation["security"]:
                    del operation["security"]
    return result


def _has_binary_field(schema_ref: dict[str, Any], full_spec: dict[str, Any]) -> bool:
    """
    Recursively checks if a schema or its sub-schemas contain a binary field.
    Handles $ref references and composition (allOf).
    """
    if not schema_ref:
        return False

    # 1. Resolve $ref if it exists
    if "$ref" in schema_ref:
        ref_path = schema_ref["$ref"].split("/")[1:]
        resolved_schema = full_spec
        for key in ref_path:
            resolved_schema = resolved_schema.get(key, {})
        return _has_binary_field(resolved_schema, full_spec)

    # 2. Check for binary format in properties
    if schema_ref.get("type") == "object" and "properties" in schema_ref:
        for prop in schema_ref["properties"].values():
            if prop.get("type") == "string" and prop.get("format") == "binary":
                return True
            if prop.get("type") == "object" or "$ref" in prop:
                if _has_binary_field(prop, full_spec):
                    return True

    # 3. Handle composition (allOf, anyOf, oneOf)
    for key in ["allOf", "anyOf", "oneOf"]:
        if key in schema_ref:
            for sub_schema in schema_ref[key]:
                if _has_binary_field(sub_schema, full_spec):
                    return True

    return False


def _get_suffix_for_media_type(media_type: str) -> str:
    """Generate a CamelCase suffix from a media type string."""
    if "multipart/form-data" in media_type:
        return "Multipart"
    if "application/x-www-form-urlencoded" in media_type:
        return "Form"
    # Fallback for other types, e.g., 'text/plain' -> 'TextPlain'
    parts = media_type.split("/")[-1].replace("-", " ").replace("+", " ").split()
    return "".join(part.capitalize() for part in parts)


def mark_optional_request_bodies(
    result: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    """
    Explicitly sets ``required: false`` on every ``requestBody`` whose schema
    has no required fields.  Many SDK generators treat the mere *presence* of
    a ``requestBody`` as mandatory unless the flag is set explicitly, even
    though the OpenAPI 3.0 spec defaults to ``false``.
    """
    schemas = result.get("components", {}).get("schemas", {})

    def _schema_has_required_fields(schema_ref: dict) -> bool:
        """Return True if the resolved schema declares any required fields."""
        if "$ref" in schema_ref:
            name = schema_ref["$ref"].split("/")[-1]
            schema_ref = schemas.get(name, {})
        if schema_ref.get("required"):
            return True
        for key in ("allOf", "oneOf", "anyOf"):
            for sub in schema_ref.get(key, []):
                if _schema_has_required_fields(sub):
                    return True
        return False

    for path_data in result.get("paths", {}).values():
        for operation in path_data.values():
            request_body = operation.get("requestBody")
            if not request_body or "content" not in request_body:
                continue
            # If already explicitly set, don't override
            if "required" in request_body:
                continue
            # Check all content-type schemas
            all_optional = True
            for media_obj in request_body["content"].values():
                schema = media_obj.get("schema", {})
                if _schema_has_required_fields(schema):
                    all_optional = False
                    break
            if all_optional:
                request_body["required"] = False

    return result


def preprocess_request_bodies(result: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """
    Pre-processes request bodies in the OpenAPI spec with a two-step logic:

    1. PRUNING:
       Removes the 'multipart/form-data' content type from an operation if its
       schema does not contain any binary fields ('type: string, format: binary').
       In such cases, 'application/json' is sufficient.

    2. DIFFERENTIATION:
       If a single schema is still used for multiple content types after pruning,
       this hook creates distinct copies of the shared schema to ensure the generator
       creates separate Python classes.
       - 'application/json' uses the original schema name.
       - Other content types (e.g., the remaining 'multipart/form-data') will reference
         a new schema with a suffix (e.g., 'MyModel' -> 'MyModelMultipart').

    This is workaround for a code generator issue:
    https://github.com/openapi-generators/openapi-python-client/issues/1276

    """
    if "components" not in result or "schemas" not in result["components"]:
        return result

    schemas = result["components"]["schemas"]

    for path_data in result.get("paths", {}).values():
        for operation in path_data.values():
            request_body = operation.get("requestBody")
            if not request_body or "content" not in request_body:
                continue

            content = request_body["content"]

            # STEP 1: PRUNING
            # If multipart exists but has no binary fields, remove it.
            for mime_type in (
                "multipart/form-data",
                "application/x-www-form-urlencoded",
            ):
                mime_schema = content.get(mime_type, {}).get("schema")
                if mime_schema and not _has_binary_field(mime_schema, result):
                    del content[mime_type]

            # If only one content type remains after pruning, no differentiation is needed.
            if len(content) <= 1:
                continue

            # STEP 2: DIFFERENTIATION
            # Find schemas ($refs) that are used by more than one content type.
            refs_to_media_types: dict[str, list[str]] = {}
            for media_type, details in content.items():
                schema = details.get("schema")
                if schema and "$ref" in schema:
                    ref_path = schema["$ref"]
                    if ref_path not in refs_to_media_types:
                        refs_to_media_types[ref_path] = []
                    refs_to_media_types[ref_path].append(media_type)

            # Process each shared reference
            for ref_path, media_types in refs_to_media_types.items():
                if len(media_types) <= 1:
                    continue  # This ref is not shared, no action needed

                try:
                    original_schema_name = ref_path.split("/")[-1]
                    original_schema = schemas.get(original_schema_name)
                    if not original_schema:
                        continue
                except IndexError:
                    continue

                # Create copies for non-JSON media types
                for media_type in media_types:
                    if media_type == "application/json":
                        continue  # Keep the original for JSON

                    suffix = _get_suffix_for_media_type(media_type)
                    new_schema_name = f"{original_schema_name}{suffix}"

                    if new_schema_name not in schemas:
                        schemas[new_schema_name] = copy.deepcopy(original_schema)

                    new_ref_path = f"#/components/schemas/{new_schema_name}"
                    content[media_type]["schema"]["$ref"] = new_ref_path
    return result


def add_polymorphic_attributes_schema(result, generator: SchemaGenerator, **kwargs):
    """
    Preprocessing hook to add polymorphic schema for the 'attributes' field
    in order creation endpoints based on offering types.
    """
    from waldur_mastermind.marketplace.plugins import manager  # test-dependency-ignore

    result_schemas = result.get("components", {}).get("schemas", {})
    offering_schemas = []

    for offering_type in manager.get_offering_types():
        processor_class = manager.get_processor(
            offering_type, "create_resource_processor"
        )
        if not processor_class:
            continue
        schema = create_offering_attributes_schema(processor_class, generator)
        if not schema:
            continue

        all_registered_components = generator.registry.build(extra_components={})
        if "schemas" in all_registered_components:
            result_schemas.update(all_registered_components["schemas"])

        schema_name = f"{offering_type.replace('.', '')}CreateOrderAttributes"
        result_schemas[schema_name] = schema
        offering_schemas.append({"$ref": f"#/components/schemas/{schema_name}"})

    result_schemas["GenericOrderAttributes"] = {
        "type": "object",
        "description": "A generic JSON object for offerings without a predefined schema. Allows any key-value pairs.",
        "additionalProperties": True,
        "properties": {
            "name": {
                "type": "string",
                "description": "The name of the resource to be created. Will be displayed in the portal.",
                "maxLength": 150,
            },
            "description": {
                "type": "string",
                "description": "A free-form description for the resource.",
            },
        },
    }

    offering_schemas.append({"$ref": "#/components/schemas/GenericOrderAttributes"})

    result_schemas["OrderCreateRequest"]["properties"]["attributes"] = {
        "oneOf": offering_schemas,
        "description": (
            "Attributes structure depends on the offering type specified in the parent object. "
            "Can also be a generic object for offerings without a specific attributes schema."
        ),
    }

    return result


def create_offering_attributes_schema(processor_class, generator):
    """
    Create schema for attributes field specific to an offering type.
    This extracts the field definitions from the processor configuration.
    """
    from waldur_mastermind.marketplace.views import (
        OrderViewSet,
    )  # test-dependency-ignore

    if getattr(processor_class, "create_serializer_class", None):
        serializer_class = processor_class.create_serializer_class
        auto = AutoSchema()
        auto.view = OrderViewSet.as_view({"post": "create"})
        auto.view.request = mock.Mock()
        auto.view.request.query_params.getlist.return_value = []
        auto.registry = generator.registry
    else:
        viewset_class = getattr(processor_class, "viewset", None)

        if not viewset_class:
            return None

        # Get the serializer class (prefer create_serializer_class)
        serializer_class = getattr(
            viewset_class, "create_serializer_class", None
        ) or getattr(viewset_class, "serializer_class", None)

        if not serializer_class:
            return None

        auto = AutoSchema()
        auto.view = viewset_class.as_view({"post": "create"})
        auto.view.request = mock.Mock()
        auto.view.request.query_params.getlist.return_value = []
        auto.registry = generator.registry

    schema = auto._map_serializer(serializer_class, direction="request")
    fields = getattr(processor_class, "fields", ())
    if fields:
        schema["properties"] = {
            field: schema["properties"][field]
            for field in fields
            if field in schema["properties"]
        }
    return schema


def inject_waldur_operation_ids(result, generator, **kwargs):
    """
    Final Attempt: Using Django Resolver to map URLFilters to OpenAPI Operation IDs.
    """
    # 1. Map: Canonical View Name -> operationId
    # We build this by resolving the actual paths discovered by spectacular.
    view_name_to_op_id = {}

    # Iterate through all discovered endpoints
    for path, path_regex, method, view in getattr(generator, "endpoints", []):
        # We only care about GET/list for the autocomplete target
        if method != "GET":
            continue

        try:
            # We need the operationId that spectacular already wrote into the result
            op_id = result["paths"][path][method.lower()]["operationId"]

            # Use Django's resolver to get the view_name (e.g., 'marketplace-offering-list')
            # We replace {uuid} with a dummy string so resolve() works
            resolve_path = re.sub(r"\{([^}]+)\}", "1", path)
            if not resolve_path.startswith("/"):
                resolve_path = "/" + resolve_path

            match = resolve(resolve_path)
            view_name_to_op_id[match.view_name] = op_id

        except (KeyError, Resolver404, Exception):
            continue

    # 2. Iterate through endpoints again to find filters and inject IDs
    for path, path_regex, method, view in getattr(generator, "endpoints", []):
        view_cls = getattr(view, "cls", None)
        if not view_cls:
            continue

        # Get the filters for this ViewSet
        filterset_class = getattr(view_cls, "filterset_class", None)
        if not filterset_class and hasattr(view_cls, "get_filterset_class"):
            try:
                filterset_class = view_cls.get_filterset_class()
            except Exception:
                continue

        if not filterset_class:
            continue

        # Map the filters defined in the FilterSet to the OpenAPI parameters
        filters = getattr(filterset_class, "base_filters", {}) or getattr(
            filterset_class, "filters", {}
        )

        for field_name, filter_obj in filters.items():
            if isinstance(
                filter_obj,
                core_filters.URLFilter
                | core_filters.RelatedUUIDFilter
                | core_filters.ModelMultipleChoiceFilter,
            ):
                target_view = getattr(filter_obj, "view_name", None)
                if not target_view:
                    continue

                # Find the Operation ID for the target view
                target_op_id = view_name_to_op_id.get(target_view)

                # Logic: If filter points to 'detail', we actually want 'list' for the UI
                if not target_op_id and target_view.endswith("-detail"):
                    target_op_id = view_name_to_op_id.get(
                        target_view.replace("-detail", "-list")
                    )

                # If target_op_id is still not found, check if target_view is a simple basename
                if not target_op_id and not target_view.endswith("-list"):
                    target_op_id = view_name_to_op_id.get(f"{target_view}-list")

                if isinstance(filter_obj, core_filters.URLFilter):
                    # For URLFilter, we want to ensure format: uri is set
                    try:
                        params = result["paths"][path][method.lower()].get(
                            "parameters", []
                        )
                        for p in params:
                            if p.get("name") == field_name:
                                schema = p.setdefault("schema", {})
                                schema["format"] = "uri"
                    except KeyError:
                        pass

                if target_op_id:
                    # Search the generated result for this parameter and inject the ID
                    try:
                        params = result["paths"][path][method.lower()].get(
                            "parameters", []
                        )
                        for p in params:
                            if p.get("name") == field_name:
                                p["x-waldur-operation-id"] = target_op_id
                    except KeyError:
                        continue

    return result


def validate_waldur_operation_ids(result, generator, **kwargs):
    """
    Enforces that all UUID query parameters (except whitelisted ones)
    must have an 'x-waldur-operation-id' defined, and that all
    'x-waldur-operation-id' values reference valid operation IDs in the schema.
    """
    errors = []
    # These fields are exempt because they usually refer to the object itself
    # or are part of a Generic Foreign Key (GFK) structure.
    whitelist = ["uuid", "scope_uuid", "scope", "parent_uuid"]

    paths = result.get("paths", {})

    # Collect all valid operation IDs from the schema
    valid_operation_ids = set()
    for path_obj in paths.values():
        for operation in path_obj.values():
            if isinstance(operation, dict) and "operationId" in operation:
                valid_operation_ids.add(operation["operationId"])

    for path, path_obj in paths.items():
        for method, operation in path_obj.items():
            if not isinstance(operation, dict):
                continue

            operation_id = operation.get("operationId", f"{method} {path}")
            parameters = operation.get("parameters", [])

            for param in parameters:
                # 1. Must be a query parameter
                if param.get("in") != "query":
                    continue

                # 2. Check if it's a UUID string
                schema = param.get("schema", {})
                is_uuid = (
                    schema.get("type") == "string" and schema.get("format") == "uuid"
                )

                if is_uuid:
                    name = param.get("name")

                    # 3. Check whitelist
                    if name in whitelist:
                        continue

                    # 4. Fail if x-waldur-operation-id is missing
                    if "x-waldur-operation-id" not in param:
                        errors.append(
                            f"Validation Error: Query parameter '{name}' in operation '{operation_id}' "
                            f"is a UUID but is missing 'x-waldur-operation-id'. "
                            f"Check if the URLFilter view_name is correct or add the field to the whitelist."
                        )

                # 5. Validate that x-waldur-operation-id references a valid operation
                ref_op_id = param.get("x-waldur-operation-id")
                if ref_op_id and ref_op_id not in valid_operation_ids:
                    name = param.get("name")
                    errors.append(
                        f"Validation Error: Query parameter '{name}' in operation '{operation_id}' "
                        f"has x-waldur-operation-id='{ref_op_id}' which does not match "
                        f"any operationId in the schema."
                    )

    if errors:
        for err in errors:
            error(err)

    return result


def _to_pascal_case(operation_id: str) -> str:
    """Convert an operation_id like 'identity_bridge_create' to 'IdentityBridgeCreate'."""
    return "".join(word.capitalize() for word in operation_id.split("_"))


def validate_go_sdk_naming_collisions(result, generator, **kwargs):
    """
    Detects naming collisions between OpenAPI schema component names and
    oapi-codegen's auto-generated response wrapper types.

    oapi-codegen generates a response wrapper struct for each operation named
    ``PascalCase(operationId) + "Response"``. If a schema component has the
    same name, the generated Go code will fail to compile due to duplicate
    type declarations.

    Example collision:
      - Schema component: ``IdentityBridgeResponse`` (from IdentityBridgeResponseSerializer)
      - Operation wrapper: ``IdentityBridgeResponse`` (from operationId ``identity_bridge``)

    Fix: rename the serializer so its schema name doesn't end with ``Response``,
    e.g. ``IdentityBridgeResultSerializer`` → ``IdentityBridgeResult``.
    """
    errors = []

    component_names = set(result.get("components", {}).get("schemas", {}).keys())
    if not component_names:
        return result

    for path_obj in result.get("paths", {}).values():
        for operation in path_obj.values():
            if not isinstance(operation, dict):
                continue

            operation_id = operation.get("operationId")
            if not operation_id:
                continue

            # oapi-codegen wrapper name: PascalCase(operationId) + "Response"
            wrapper_name = _to_pascal_case(operation_id) + "Response"

            if wrapper_name in component_names:
                errors.append(
                    f"Go SDK naming collision: schema component '{wrapper_name}' "
                    f"collides with oapi-codegen response wrapper for operation "
                    f"'{operation_id}'. Rename the serializer to avoid the "
                    f"'*Response' suffix (e.g. use '*Result' instead)."
                )

    if errors:
        for err in errors:
            error(err)

    return result


def extract_query_enums(result, generator, **kwargs):
    """
    Extracts inline enum definitions from query parameters into reusable components.
    Naming logic: [ResponseModelName][ParameterName]Enum
    """
    schemas = result.setdefault("components", {}).setdefault("schemas", {})

    def get_response_model_name(operation):
        """Finds the name of the main model returned by a 200 OK response."""
        try:
            # Check 200 or 201 responses
            for code in ["200", "201"]:
                res = operation.get("responses", {}).get(code, {})
                content = res.get("content", {}).get("application/json", {})
                schema = content.get("schema", {})

                # If it's a direct reference
                if "$ref" in schema:
                    return schema["$ref"].split("/")[-1]

                # If it's a paginated list or array
                if schema.get("type") == "array" and "$ref" in schema.get("items", {}):
                    return schema["items"]["$ref"].split("/")[-1]
        except (KeyError, AttributeError, IndexError):
            pass
        return None

    def find_existing_enum_name(enum_values, enum_descriptions=None):
        """Returns the name of an existing schema that matches these enum values and descriptions."""
        sorted_values = sorted([str(v) for v in enum_values])
        for name, schema in schemas.items():
            if "enum" in schema:
                if sorted([str(v) for v in schema["enum"]]) == sorted_values:
                    if schema.get("x-enum-descriptions") == enum_descriptions:
                        return name
        return None

    for path in result.get("paths", {}).values():
        for operation in path.values():
            if not isinstance(operation, dict) or "parameters" not in operation:
                continue

            model_name = get_response_model_name(operation)

            for param in operation["parameters"]:
                if param.get("in") != "query":
                    continue

                schema = param.get("schema", {})
                # Handle both direct enum and array items enum
                is_array = schema.get("type") == "array"
                target = schema.get("items", {}) if is_array else schema

                if "enum" in target:
                    enum_values = target["enum"]
                    enum_descriptions = param.get("x-enum-descriptions")

                    # 1. Try to find an existing identical enum to reuse
                    existing_name = find_existing_enum_name(
                        enum_values, enum_descriptions
                    )

                    if existing_name:
                        target_ref_name = existing_name
                    else:
                        # 2. Generate a new name: Model + Field + Enum
                        field_name = (
                            param["name"].replace("_", " ").title().replace(" ", "")
                        )
                        prefix = model_name if model_name else "Query"
                        target_ref_name = f"{prefix}{field_name}Enum"

                        # Handle name collisions with different values
                        counter = 1
                        base_name = target_ref_name
                        while target_ref_name in schemas and (
                            schemas[target_ref_name].get("enum") != enum_values
                            or schemas[target_ref_name].get("x-enum-descriptions")
                            != enum_descriptions
                        ):
                            target_ref_name = f"{base_name}{counter}"
                            counter += 1

                        # 3. Register the new component
                        schemas[target_ref_name] = {
                            "type": "string",
                            "enum": enum_values,
                            "description": "",
                        }
                        if enum_descriptions:
                            schemas[target_ref_name]["x-enum-descriptions"] = (
                                enum_descriptions
                            )

                    # 4. Replace inline definition with $ref
                    # Remove keys that would conflict with $ref (OpenAPI 3.0 rules)
                    for key in ["type", "enum", "items"]:
                        if key in target:
                            del target[key]

                    target["$ref"] = f"#/components/schemas/{target_ref_name}"

                    if "x-enum-descriptions" in param:
                        del param["x-enum-descriptions"]

    return result


def sanitize_schema(result, generator, **kwargs):
    """
    Fix common OpenAPI 3.0 validation issues:

    1. Remove empty 'required' arrays from schema components (minItems: 1 required).
    2. Remove 'schema: null' from response content types (must be an object).
    """
    for schemas in result.get("components", {}).values():
        for schema in schemas.values():
            if isinstance(schema, dict) and schema.get("required") == []:
                del schema["required"]

    for path_methods in result.get("paths", {}).values():
        for operation in path_methods.values():
            if not isinstance(operation, dict):
                continue
            for response in operation.get("responses", {}).values():
                if not isinstance(response, dict):
                    continue
                for media_type in response.get("content", {}).values():
                    if (
                        isinstance(media_type, dict)
                        and media_type.get("schema") is None
                    ):
                        del media_type["schema"]

    return result


def check_action_responses(result, generator, **kwargs):
    """
    Ensure that all custom DRF actions have explicitly defined responses.
    This prevents drf-spectacular from defaulting to 200 OK + the viewset serializer,
    which is often incorrect for custom actions that return status dicts or empty responses.
    """
    import inspect

    errors = []

    for path, path_regex, method, callback in generator.endpoints:
        if not hasattr(callback, "cls") or not hasattr(callback, "actions"):
            continue

        viewset_class = callback.cls
        action_name = callback.actions.get(method.lower())

        if not action_name or action_name in [
            "list",
            "retrieve",
            "create",
            "update",
            "partial_update",
            "destroy",
        ]:
            continue

        action_method = getattr(viewset_class, action_name, None)
        if not action_method:
            continue

        try:
            # Unwrap method to get original source if decorated
            while hasattr(action_method, "__wrapped__"):
                action_method = action_method.__wrapped__
            source = inspect.getsource(action_method)
        except (TypeError, OSError):
            continue

        if "responses=" not in source:
            errors.append(
                f"Action '{action_name}' on {viewset_class.__name__} must specify responses explicitly via @extend_schema(responses=...)."
            )

    if errors:
        for err in errors:
            error(err)

    return result
