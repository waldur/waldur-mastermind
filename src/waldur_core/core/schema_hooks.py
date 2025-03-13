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
