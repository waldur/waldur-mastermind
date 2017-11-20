from django.utils.functional import cached_property

from nodeconductor.structure import models as structure_models
from nodeconductor.structure.tests import fixtures as structure_fixtures, factories as structure_factories
from waldur_mastermind.packages import models as package_models
from waldur_openstack.openstack import apps as openstack_apps, models as openstack_models
from waldur_openstack.openstack_tenant import apps as tenant_apps, models as tenant_models
from waldur_openstack.openstack_tenant.tests import factories as tenant_factories

Types = package_models.PackageComponent.Types


class EstimationFixture(structure_fixtures.ProjectFixture):

    @cached_property
    def shared_settings(self):
        return structure_factories.ServiceSettingsFactory(
            type=openstack_apps.OpenStackConfig.service_name,
            shared=True,
            options={'external_network_id': 'test_network_id'},
            state=structure_models.ServiceSettings.States.OK,
        )

    @cached_property
    def prices(self):
        return {
            Types.CORES: 10,
            Types.RAM: 20,
            Types.STORAGE: 30,
        }

    @cached_property
    def template(self):
        template = package_models.PackageTemplate.objects.create(
            name='Premium package template',
            service_settings=self.shared_settings
        )
        for (type, price) in self.prices.items():
            package_models.PackageComponent.objects.create(
                type=type,
                price=price,
                template=template
            )
        return template

    @cached_property
    def shared_service(self):
        return openstack_models.OpenStackService.objects.get(
            customer=self.customer,
            settings=self.shared_settings,
        )

    @cached_property
    def shared_link(self):
        return openstack_models.OpenStackServiceProjectLink.objects.get(
            service=self.shared_service,
            project=self.project,
        )

    @cached_property
    def tenant(self):
        return openstack_models.Tenant.objects.create(
            name='Tenant',
            service_project_link=self.shared_link,
            extra_configuration={
                'package_uuid': self.template.uuid.hex
            }
        )

    @cached_property
    def private_settings(self):
        return structure_factories.ServiceSettingsFactory(
            type=tenant_apps.OpenStackTenantConfig.service_name,
            customer=self.customer,
            scope=self.tenant,
            options={
                'availability_zone': self.tenant.availability_zone,
                'tenant_id': self.tenant.backend_id,
                'external_network_id': self.tenant.external_network_id,
                'internal_network_id': self.tenant.internal_network_id,
            }
        )

    @cached_property
    def private_service(self):
        return tenant_models.OpenStackTenantService.objects.create(
            settings=self.private_settings,
            customer=self.customer,
        )

    @cached_property
    def private_link(self):
        return tenant_models.OpenStackTenantServiceProjectLink.objects.create(
            service=self.private_service,
            project=self.project,
        )

    @cached_property
    def image(self):
        return tenant_factories.ImageFactory(
            settings=self.private_settings,
            min_disk=10240,
            min_ram=1024
        )

    @cached_property
    def flavor(self):
        return tenant_factories.FlavorFactory(settings=self.private_settings)

    @cached_property
    def network(self):
        return tenant_models.Network.objects.create(settings=self.private_settings)

    @cached_property
    def subnet(self):
        return tenant_models.SubNet.objects.create(
            settings=self.private_settings,
            network=self.network,
        )

    @cached_property
    def ssh_public_key(self):
        return structure_factories.SshPublicKeyFactory(user=self.owner)
