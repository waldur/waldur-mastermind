from django.utils.functional import cached_property

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_openstack.openstack import models as openstack_models, apps as openstack_apps
from waldur_openstack.openstack.tests.factories import TenantFactory
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps

from . import factories


class OpenStackFixture(ProjectFixture):
    @cached_property
    def openstack_service_settings(self):
        # OpenStack packages should be used only with shared settings.
        return structure_factories.ServiceSettingsFactory(
            type=openstack_apps.OpenStackConfig.service_name,
            shared=True,
            options={'external_network_id': 'test_network_id'},
            state=structure_models.ServiceSettings.States.OK,
        )

    @cached_property
    def openstack_service(self):
        return openstack_models.OpenStackService.objects.create(
            customer=self.customer,
            settings=self.openstack_service_settings,
        )

    @cached_property
    def openstack_spl(self):
        return openstack_models.OpenStackServiceProjectLink.objects.create(
            project=self.project, service=self.openstack_service)

    @cached_property
    def openstack_tenant(self):
        return TenantFactory(
            service_project_link=self.openstack_spl,
            state=openstack_models.Tenant.States.OK,
        )


class PackageFixture(OpenStackFixture):
    @cached_property
    def openstack_template(self):
        return factories.PackageTemplateFactory(service_settings=self.openstack_service_settings)

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
                }
            )
        )
