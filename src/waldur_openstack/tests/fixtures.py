from unittest import mock

import factory
from django.utils.functional import cached_property

from waldur_core.core.enums import CoreStates
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
            state=CoreStates.OK,
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
            state=CoreStates.OK,
        )

    @cached_property
    def subnet(self):
        return factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.project,
            state=CoreStates.OK,
            backend_id=factory.Sequence(lambda n: "subnet_%s" % n),
        )

    @cached_property
    def router(self):
        return factories.RouterFactory(
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.project,
            state=CoreStates.OK,
        )

    @cached_property
    def floating_ip(self):
        return factories.FloatingIPFactory(
            service_settings=self.settings,
            project=self.project,
            tenant=self.tenant,
            state=CoreStates.OK,
        )

    @cached_property
    def security_group(self):
        return factories.SecurityGroupFactory(
            service_settings=self.settings,
            project=self.project,
            tenant=self.tenant,
            state=CoreStates.OK,
        )

    @cached_property
    def security_group_rule(self):
        return factories.SecurityGroupRuleFactory(
            security_group=self.security_group,
        )

    @cached_property
    def server_group(self):
        return factories.ServerGroupFactory(
            service_settings=self.settings,
            project=self.project,
            tenant=self.tenant,
            state=CoreStates.OK,
        )

    @cached_property
    def volume_type(self):
        volume_type = factories.VolumeTypeFactory(settings=self.settings)
        volume_type.tenants.add(self.tenant)
        return volume_type

    @cached_property
    def port(self):
        return factories.PortFactory(
            network=self.network,
            tenant=self.tenant,
            subnet=self.subnet,
            service_settings=self.settings,
            project=self.project,
            state=CoreStates.OK,
            instance=self.instance,
        )

    @cached_property
    def volume(self):
        return factories.VolumeFactory(
            project=self.project,
            tenant=self.tenant,
            state=CoreStates.OK,
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
            state=CoreStates.OK,
            runtime_state=models.Instance.RuntimeStates.SHUTOFF,
        )

    @cached_property
    def snapshot(self):
        return factories.SnapshotFactory(
            project=self.project,
            tenant=self.tenant,
            state=CoreStates.OK,
            runtime_state=models.Volume.RuntimeStates.OFFLINE,
            source_volume=self.volume,
        )

    @cached_property
    def backup(self):
        return factories.BackupFactory(
            project=self.project,
            tenant=self.tenant,
            instance=self.instance,
        )

    @cached_property
    def volume_availability_zone(self):
        return factories.VolumeAvailabilityZoneFactory(tenant=self.tenant)

    @cached_property
    def flavor(self):
        flavor = factories.FlavorFactory(settings=self.settings)
        flavor.tenants.add(self.tenant)
        return flavor

    @cached_property
    def image(self):
        image = factories.ImageFactory(
            settings=self.settings, min_disk=10240, min_ram=1024
        )
        image.tenants.add(self.tenant)
        return image


def mock_session():
    session_mock = mock.patch("keystoneauth1.session.Session").start()()
    session_mock.auth.auth_url = "auth_url"
    session_mock.auth.project_id = "project_id"
    session_mock.auth.project_domain_name = None
    session_mock.auth.project_name = None
    session_mock.auth.auth_ref.auth_token = "token"
    session_mock.auth.get_auth_state.return_value = ""
