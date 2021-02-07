from django.utils.functional import cached_property

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace_openstack.tests import factories
from waldur_openstack.openstack_base.tests.fixtures import OpenStackFixture
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps


class PackageFixture(OpenStackFixture):
    @cached_property
    def openstack_template(self):
        return factories.PackageTemplateFactory(
            service_settings=self.openstack_service_settings
        )

    @cached_property
    def openstack_package(self):
        return factories.OpenStackPackageFactory(
            tenant=self.openstack_tenant,
            template=self.openstack_template,
            service_settings=structure_factories.ServiceSettingsFactory(
                customer=self.customer,
                type=openstack_tenant_apps.OpenStackTenantConfig.service_name,
                scope=self.openstack_tenant,
                options={
                    'availability_zone': self.openstack_tenant.availability_zone,
                    'tenant_id': self.openstack_tenant.backend_id,
                    'external_network_id': self.openstack_tenant.external_network_id,
                    'internal_network_id': self.openstack_tenant.internal_network_id,
                },
            ),
        )
