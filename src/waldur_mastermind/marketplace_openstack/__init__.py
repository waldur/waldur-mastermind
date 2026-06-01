RAM_TYPE = "ram"
CORES_TYPE = "cores"
STORAGE_TYPE = "storage"

AVAILABLE_LIMITS = [RAM_TYPE, CORES_TYPE, STORAGE_TYPE]


def get_mb_component_types() -> frozenset[str]:
    """Return component types stored in MB (factor=1024) derived from TENANT_COMPONENTS."""
    from waldur_mastermind.marketplace_openstack.const import TENANT_COMPONENTS

    return frozenset(c.type for c in TENANT_COMPONENTS if c.factor == 1024)


STORAGE_MODE_FIXED = "fixed"
STORAGE_MODE_DYNAMIC = "dynamic"
