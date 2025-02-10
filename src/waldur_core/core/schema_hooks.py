from waldur_core.core.api_groups_mapping import API_GROUPS, VALID_ENDPOINTS


def preprocess_filter_api_groups(endpoints, **kwargs):
    return [
        (path, path_regex, method, callback)
        for path, path_regex, method, callback in endpoints
        if any(path.startswith(endpoint) for endpoint in VALID_ENDPOINTS)
    ]


def postprocess_drop_path_description(result, generator, **kwargs):
    for methods in result.get("paths", {}).values():
        for operation in methods.values():
            operation["description"] = ""
    return result


def postprocess_add_tag(result, generator, **kwargs):
    for api_group, endpoints in API_GROUPS.items():
        for endpoint in endpoints:
            for path, methods in result.get("paths", {}).items():
                if path.startswith(endpoint):
                    for operation in methods.values():
                        operation["tags"] = [api_group]
    return result
