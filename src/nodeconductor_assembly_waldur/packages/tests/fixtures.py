from django.utils.functional import cached_property
from nodeconductor.structure.tests import factories as structure_factories
from nodeconductor.structure.tests.fixtures import ProjectFixture
from nodeconductor_openstack import models as openstack_models, apps as openstack_apps

from . import factories


class OpenStackFixture(ProjectFixture):
    @cached_property
    def openstack_service_settings(self):
        return structure_factories.ServiceSettingsFactory(
            type=openstack_apps.OpenStackConfig.service_name,
            customer=self.customer
        )

    @cached_property
    def openstack_service(self):
        return openstack_models.OpenStackService.objects.create(
            customer=self.customer,
            settings=self.openstack_service_settings,
            name=self.openstack_service_settings.name
        )

    @cached_property
    def openstack_spl(self):
        return openstack_models.OpenStackServiceProjectLink.objects.create(
            project=self.project, service=self.openstack_service)

    @cached_property
    def openstack_tenant(self):
        return openstack_models.Tenant.objects.create(service_project_link=self.openstack_spl)


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
                type=openstack_apps.OpenStackConfig.service_name,
                scope=self.openstack_tenant)
        )
