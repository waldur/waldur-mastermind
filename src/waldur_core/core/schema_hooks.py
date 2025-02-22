from waldur_core.core.api_groups_mapping import API_GROUPS


def postprocess_drop_description(result, generator, **kwargs):
    """
    Remove descriptions from OpenAPI schema paths and components.
    """
    for methods in result.get("components", {}).values():
        for operation in methods.values():
            operation["description"] = ""
    return result


def postprocess_add_tag(result, generator, **kwargs):
    """
    Post-process OpenAPI schema to add tags to operations based on API groups.
    """
    for api_group, endpoints in API_GROUPS.items():
        for endpoint in endpoints:
            for path, methods in result.get("paths", {}).items():
                if path.startswith(endpoint):
                    for operation in methods.values():
                        operation["tags"] = [api_group]
    return result
