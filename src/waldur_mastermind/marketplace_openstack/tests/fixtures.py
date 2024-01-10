from django.utils.functional import cached_property

from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.openstack_base.tests.fixtures import OpenStackFixture
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps


class MarketplaceOpenStackFixture(OpenStackFixture):
    @cached_property
    def private_settings(self):
        return structure_factories.ServiceSettingsFactory(
            customer=self.customer,
            type=openstack_tenant_apps.OpenStackTenantConfig.service_name,
            scope=self.openstack_tenant,
            options={
                "availability_zone": self.openstack_tenant.availability_zone,
                "tenant_id": self.openstack_tenant.backend_id,
                "external_network_id": self.openstack_tenant.external_network_id,
                "internal_network_id": self.openstack_tenant.internal_network_id,
            },
        )
