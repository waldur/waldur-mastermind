from waldur_mastermind.marketplace.registrators import MarketplaceRegistrator
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    SHARED_INSTANCE_TYPE,
    STORAGE_TYPE,
    TENANT_TYPE,
)
from waldur_openstack.openstack_base.utils import is_valid_volume_type_name


class OpenStackBaseRegistrator(MarketplaceRegistrator):
    @classmethod
    def convert_quantity(cls, usage, component_type: str):
        if component_type in (STORAGE_TYPE, RAM_TYPE):
            return int(usage / 1024)
        return int(usage)

    @classmethod
    def get_component_name(cls, plan_component):
        component_type = plan_component.component.type
        if component_type == CORES_TYPE:
            return "CPU"
        elif component_type == RAM_TYPE:
            return "RAM"
        elif component_type == STORAGE_TYPE:
            return "storage"
        elif is_valid_volume_type_name(component_type):
            return f'{component_type.replace("gigabytes_", "")} storage'
        else:
            return plan_component.component.name


class OpenStackTenantRegistrator(OpenStackBaseRegistrator):
    plugin_name = TENANT_TYPE


class OpenStackInstanceRegistrator(OpenStackBaseRegistrator):
    plugin_name = SHARED_INSTANCE_TYPE
