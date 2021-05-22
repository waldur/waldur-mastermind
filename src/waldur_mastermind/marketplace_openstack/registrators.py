from waldur_mastermind.marketplace.registrators import MarketplaceRegistrator
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_TYPE,
    TENANT_TYPE,
)


class OpenStackRegistrator(MarketplaceRegistrator):
    plugin_name = TENANT_TYPE

    @classmethod
    def convert_quantity(cls, usage, component_type: str):
        if component_type in (STORAGE_TYPE, RAM_TYPE):
            return int(usage / 1024)
        return int(usage)

    @classmethod
    def get_component_name(cls, plan_component):
        component_type = plan_component.component.type
        if component_type == CORES_TYPE:
            return 'CPU'
        elif component_type == RAM_TYPE:
            return 'RAM'
        elif component_type == STORAGE_TYPE:
            return 'storage'
        elif component_type.startswith('gigabytes_'):
            return f'{component_type.replace("gigabytes_", "")} storage'
        else:
            return plan_component.component.name
