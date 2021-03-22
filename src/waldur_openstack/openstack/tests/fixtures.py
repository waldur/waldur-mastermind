from django.utils.functional import cached_property

from waldur_core.structure.tests.fixtures import ProjectFixture

from .. import models
from . import factories


class OpenStackFixture(ProjectFixture):
    @cached_property
    def openstack_service_settings(self):
        return factories.OpenStackServiceSettingsFactory(customer=self.customer)

    @cached_property
    def tenant(self):
        return factories.TenantFactory(
            service_settings=self.openstack_service_settings, project=self.project
        )

    @cached_property
    def network(self):
        return factories.NetworkFactory(
            service_settings=self.openstack_service_settings,
            project=self.project,
            tenant=self.tenant,
            state=models.Network.States.OK,
        )

    @cached_property
    def subnet(self):
        return factories.SubNetFactory(
            network=self.network,
            service_settings=self.openstack_service_settings,
            project=self.project,
            state=models.SubNet.States.OK,
        )

    @cached_property
    def floating_ip(self):
        return factories.FloatingIPFactory(
            service_settings=self.openstack_service_settings,
            project=self.project,
            tenant=self.tenant,
            state=models.FloatingIP.States.OK,
        )

    @cached_property
    def security_group(self):
        return factories.SecurityGroupFactory(
            service_settings=self.openstack_service_settings,
            project=self.project,
            tenant=self.tenant,
            state=models.SecurityGroup.States.OK,
        )

    @cached_property
    def volume_type(self):
        return factories.VolumeTypeFactory(settings=self.openstack_service_settings)

    @cached_property
    def port(self):
        return factories.PortFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.openstack_service_settings,
            project=self.project,
            state=models.Port.States.OK,
        )
