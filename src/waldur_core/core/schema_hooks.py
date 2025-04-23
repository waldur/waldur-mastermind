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
                    and parameter["schema"]["items"]["type"] == "integer"
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


def adjust_request_body_content_types(result, generator, **kwargs):
    """
    Adjusts the content types for POST requests based on the presence of binary fields.
    """

    # Iterate through all paths
    for path in result["paths"].values():
        for operation in path.values():
            request_body = operation.get("requestBody")
            if not request_body:
                continue
            if "application/json" in request_body["content"]:
                request_body["content"].pop("application/x-www-form-urlencoded", None)
                request_body["content"].pop("multipart/form-data", None)
    return result
