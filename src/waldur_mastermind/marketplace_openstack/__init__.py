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

# Source for OpenStack instance compute ComponentUsage.
# QUOTA: cores/ram from the tenant's flavor-derived Nova quota usage (default).
# PLACEMENT: cores/ram and specialty resource classes (VGPU, PCI_DEVICE, custom)
# from Placement allocations. Storage stays on Cinder quota in both modes.
BILLING_SOURCE_QUOTA = "quota"
BILLING_SOURCE_PLACEMENT = "placement"

# Whether the cores and RAM of servers that are not running are billed. On by
# default; an offering turns it off to bill compute for running servers only.
# Storage is billed either way, because the volumes of a stopped server remain.
BILL_INACTIVE_INSTANCES_OPTION = "bill_inactive_instances"
# Nova statuses of a server that is not running. PAUSED keeps the server's
# memory on the host, and ERROR or a transitional status says nothing about
# whether it runs, so those stay billed.
INACTIVE_INSTANCE_STATES = frozenset(
    ("SHUTOFF", "SUSPENDED", "SHELVED", "SHELVED_OFFLOADED")
)

# Custom metrics an OpenStack offering can collect for the instances in its
# tenants, and for the volumes attached to them. Turned on, and chosen, in the
# offering's plugin options.
INSTANCE_METRICS_ENABLED_OPTION = "instance_metrics_enabled"
INSTANCE_METRICS_OPTION = "instance_metrics"
INSTANCE_METRIC_KEYS = (
    "vm.cpu.utilization",
    "vm.cpu.time",
    "vm.memory.utilization",
    "vm.network.io",
    "vm.disk.io",
    "vm.disk.operations",
)
VOLUME_METRIC_KEYS = ("volume.io", "volume.operations")
