from django.utils.functional import cached_property

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_openstack.openstack.tests import fixtures as openstack_fixtures

from . import factories
from .. import models


class OpenStackTenantFixture(ProjectFixture):

    @cached_property
    def openstack_tenant_service_settings(self):
        # has to be cached, otherwise all referenced objects are not going to be cached (e.g. scope).
        openstack_fixture = openstack_fixtures.OpenStackFixture()

        return factories.OpenStackTenantServiceSettingsFactory(
            name=openstack_fixture.tenant.name,
            scope=openstack_fixture.tenant,
            customer=self.customer,
            backend_url=openstack_fixture.openstack_service_settings.backend_url,
            username=openstack_fixture.tenant.user_username,
            password=openstack_fixture.tenant.user_password,
            options={
                'availability_zone': openstack_fixture.tenant.availability_zone,
                'tenant_id': openstack_fixture.tenant.backend_id,
            },
        )

    @cached_property
    def openstack_tenant_service(self):
        return factories.OpenStackTenantServiceFactory(
            customer=self.customer,
            settings=self.openstack_tenant_service_settings
        )

    @cached_property
    def network(self):
        return factories.NetworkFactory(settings=self.openstack_tenant_service_settings)

    @cached_property
    def subnet(self):
        return factories.SubNetFactory(network=self.network, settings=self.openstack_tenant_service_settings)

    @cached_property
    def spl(self):
        return factories.OpenStackTenantServiceProjectLinkFactory(
            project=self.project, service=self.openstack_tenant_service)

    @cached_property
    def volume(self):
        return factories.VolumeFactory(
            service_project_link=self.spl,
            state=models.Volume.States.OK,
            runtime_state=models.Volume.RuntimeStates.OFFLINE,
        )

    @cached_property
    def instance(self):
        return factories.InstanceFactory(
            service_project_link=self.spl,
            state=models.Instance.States.OK,
            runtime_state=models.Instance.RuntimeStates.SHUTOFF,
        )

    @cached_property
    def snapshot(self):
        return factories.SnapshotFactory(
            service_project_link=self.spl,
            state=models.Volume.States.OK,
            runtime_state=models.Volume.RuntimeStates.OFFLINE,
            source_volume=self.volume,
        )

    @cached_property
    def backup(self):
        return factories.BackupFactory(
            service_project_link=self.spl,
            instance=self.instance,
            backup_schedule=self.backup_schedule
        )

    @cached_property
    def backup_schedule(self):
        return factories.BackupScheduleFactory(
            service_project_link=self.spl,
            state=models.BackupSchedule.States.OK,
            instance=self.instance,
        )

    @cached_property
    def snapshot_schedule(self):
        return factories.SnapshotScheduleFactory(
            service_project_link=self.spl,
            state=models.SnapshotSchedule.States.OK,
            source_volume=self.volume,
        )

    @cached_property
    def floating_ip(self):
        return factories.FloatingIPFactory(
            settings=self.openstack_tenant_service_settings,
            runtime_state='DOWN',
        )

    @cached_property
    def internal_ip(self):
        return factories.InternalIPFactory(
            subnet=self.subnet,
            instance=self.instance,
        )
