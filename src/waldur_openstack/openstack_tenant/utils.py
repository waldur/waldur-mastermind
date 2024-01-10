def get_valid_availability_zones(instance):
    """
    Fetch valid availability zones for instance or volume from shared settings.
    """
    tenant = instance.service_settings.scope
    if tenant:
        return tenant.service_settings.options.get("valid_availability_zones") or {}
    return {}
