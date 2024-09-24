from unittest import mock

from django.utils.functional import cached_property

from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_openstack import models
from waldur_openstack.tests import factories


class OpenStackFixture(ProjectFixture):
    @cached_property
    def settings(self):
        return factories.SettingsFactory(
            customer=self.customer,
            shared=True,
            options={"external_network_id": "test_network_id"},
            state=ServiceSettings.States.OK,
        )

    @cached_property
    def tenant(self):
        return factories.TenantFactory(
            service_settings=self.settings, project=self.project
        )

    @cached_property
    def network(self):
        return factories.NetworkFactory(
            service_settings=self.settings,
            project=self.project,
            tenant=self.tenant,
            state=models.Network.States.OK,
        )

    @cached_property
    def subnet(self):
        return factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.project,
            state=models.SubNet.States.OK,
        )

    @cached_property
    def floating_ip(self):
        return factories.FloatingIPFactory(
            service_settings=self.settings,
            project=self.project,
            tenant=self.tenant,
            state=models.FloatingIP.States.OK,
        )

    @cached_property
    def security_group(self):
        return factories.SecurityGroupFactory(
            service_settings=self.settings,
            project=self.project,
            tenant=self.tenant,
            state=models.SecurityGroup.States.OK,
        )

    @cached_property
    def server_group(self):
        return factories.ServerGroupFactory(
            service_settings=self.settings,
            project=self.project,
            tenant=self.tenant,
            state=models.ServerGroup.States.OK,
        )

    @cached_property
    def volume_type(self):
        return factories.VolumeTypeFactory(settings=self.settings)

    @cached_property
    def port(self):
        return factories.PortFactory(
            network=self.network,
            tenant=self.tenant,
            subnet=self.subnet,
            service_settings=self.settings,
            project=self.project,
            state=models.Port.States.OK,
            instance=self.instance,
        )

    @cached_property
    def volume(self):
        return factories.VolumeFactory(
            project=self.project,
            tenant=self.tenant,
            state=models.Volume.States.OK,
            runtime_state=models.Volume.RuntimeStates.OFFLINE,
            type=self.volume_type,
            availability_zone=self.volume_availability_zone,
        )

    @cached_property
    def instance_availability_zone(self):
        return factories.InstanceAvailabilityZoneFactory(tenant=self.tenant)

    @cached_property
    def instance(self):
        return factories.InstanceFactory(
            project=self.project,
            tenant=self.tenant,
            state=models.Instance.States.OK,
            runtime_state=models.Instance.RuntimeStates.SHUTOFF,
        )

    @cached_property
    def snapshot(self):
        return factories.SnapshotFactory(
            project=self.project,
            tenant=self.tenant,
            state=models.Volume.States.OK,
            runtime_state=models.Volume.RuntimeStates.OFFLINE,
            source_volume=self.volume,
        )

    @cached_property
    def backup(self):
        return factories.BackupFactory(
            project=self.project,
            tenant=self.tenant,
            instance=self.instance,
            backup_schedule=self.backup_schedule,
        )

    @cached_property
    def backup_schedule(self):
        return factories.BackupScheduleFactory(
            project=self.project,
            tenant=self.tenant,
            state=models.BackupSchedule.States.OK,
            instance=self.instance,
        )

    @cached_property
    def snapshot_schedule(self):
        return factories.SnapshotScheduleFactory(
            project=self.project,
            tenant=self.tenant,
            state=models.SnapshotSchedule.States.OK,
            source_volume=self.volume,
        )

    @cached_property
    def volume_availability_zone(self):
        return factories.VolumeAvailabilityZoneFactory(tenant=self.tenant)

    @cached_property
    def flavor(self):
        return factories.FlavorFactory(settings=self.settings)


def mock_session():
    session_mock = mock.patch("keystoneauth1.session.Session").start()()
    session_mock.auth.auth_url = "auth_url"
    session_mock.auth.project_id = "project_id"
    session_mock.auth.project_domain_name = None
    session_mock.auth.project_name = None
    session_mock.auth.auth_ref.auth_token = "token"
    session_mock.auth.get_auth_state.return_value = ""
