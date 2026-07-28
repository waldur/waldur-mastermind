import datetime
import uuid
from unittest import mock

from cinderclient import exceptions as cinder_exceptions
from cinderclient.v2.volumes import Volume
from ddt import data, ddt
from django.test import TestCase
from django.utils import timezone
from neutronclient.common import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions
from novaclient.v2.flavors import Flavor
from novaclient.v2.servers import Server

from waldur_core.core.models import CoreStates
from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.models import Port
from waldur_openstack.tests.factories import (
    FloatingIPFactory,
    ImageFactory,
    PortFactory,
    VolumeTypeFactory,
)
from waldur_openstack.tests.fixtures import mock_session

from . import factories, fixtures


class BaseBackendTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.openstack_settings = self.tenant.service_settings
        self.mocked_neutron = mock.patch("neutronclient.v2_0.client.Client").start()()
        self.mocked_cinder = mock.patch("cinderclient.v3.client.Client").start()()
        self.mocked_nova = mock.patch("novaclient.v2.client.Client").start()()
        self.mocked_glance = mock.patch("glanceclient.v2.client.Client").start()()
        self.backend = OpenStackBackend(self.tenant.service_settings)
        mock_session()

    def tearDown(self) -> None:
        super().tearDown()
        mock.patch.stopall()

    def _get_valid_volume(self, backend_id):
        return Volume(
            manager=None,
            info=dict(
                name="volume-%s" % backend_id,
                size=1,
                metadata="",
                description="",
                volume_type="",
                status="OK",
                id=backend_id,
                bootable="true",
            ),
        )

    def _get_valid_instance(self, backend_id):
        return Server(
            manager=None,
            info={
                "id": backend_id,
                "name": "server-%s" % backend_id,
                "status": "ACTIVE",
                "key_name": "",
                "created": "2012-04-23T08:10:00Z",
                "OS-SRV-USG:launched_at": "2012-04-23T09:15",
                "flavor": {
                    "vcpus": 2,
                    "ram": 4096,
                    "disk": 10,
                    "ephemeral": 0,
                    "swap": 0,
                    "original_name": "m1.small",
                },
                "image": {"id": backend_id},
                "networks": {
                    "test-int-net": ["192.168.42.60"],
                    "public": ["172.29.249.185"],
                },
            },
        )

    def _get_valid_flavor(self, backend_id):
        return Flavor(
            manager=None,
            info=dict(
                name="m1.small",
                disk=10,
                vcpus=2,
                ram=4096,
                id=backend_id,
            ),
        )

    def _get_valid_image(self, backend_id):
        return dict(
            name="Ubuntu",
            id=backend_id,
        )


class VolumesBaseTest(BaseBackendTest):
    def _generate_volumes(self, backend=False, count=1):
        volumes = []
        for i in range(count):
            volume = factories.VolumeFactory()
            backend_volume = self._get_valid_volume(backend_id=volume.backend_id)
            if backend:
                volume.delete()
            volumes.append(backend_volume)

        return volumes


class GetVolumesTest(VolumesBaseTest):
    def test_all_backend_volumes_are_returned(self):
        backend_volumes = self._generate_volumes(backend=True, count=2)
        volumes = backend_volumes + self._generate_volumes()
        self.mocked_cinder.volumes.list.return_value = volumes

        result = self.backend.get_volumes(self.tenant)

        returned_backend_ids = [item.backend_id for item in result]
        expected_backend_ids = [item.id for item in volumes]
        self.assertEqual(sorted(returned_backend_ids), sorted(expected_backend_ids))


class CreateVolumesTest(VolumesBaseTest):
    def setUp(self):
        super().setUp()
        self.mocked_cinder.volumes.create.return_value = self._generate_volumes()[0]

    def test_use_default_volume_type_if_type_not_populated(self):
        volume_type = VolumeTypeFactory(settings=self.tenant.service_settings)
        self.tenant.default_volume_type_name = volume_type.name
        self.tenant.save()
        volume = self._get_volume()
        self.assertEqual(volume.type.name, volume_type.name)

    @mock.patch("waldur_openstack.backend.logger")
    def test_not_use_default_volume_type_if_it_not_exists(self, mock_logger):
        self.tenant.default_volume_type_name = "not_exists_value_type"
        self.tenant.save()
        volume = self._get_volume()
        self.assertEqual(volume.type, None)
        mock_logger.error.assert_called_once()

    @mock.patch("waldur_openstack.backend.logger")
    def test_not_use_default_volume_type_if_two_types_exist(self, mock_logger):
        volume_type = VolumeTypeFactory(settings=self.tenant.service_settings)
        VolumeTypeFactory(name=volume_type.name, settings=self.tenant.service_settings)
        self.tenant.default_volume_type_name = volume_type.name
        self.tenant.save()
        volume = self._get_volume()
        self.assertEqual(volume.type, None)
        mock_logger.error.assert_called_once()

    def test_use_default_volume_availability_zone_if_zone_not_populated(self):
        volume_availability_zone = factories.VolumeAvailabilityZoneFactory(
            settings=self.openstack_settings
        )
        self.tenant.service_settings.options["volume_availability_zone_name"] = (
            volume_availability_zone.name
        )
        self.tenant.service_settings.save()
        volume = self._get_volume()
        self.assertEqual(volume.availability_zone.name, volume_availability_zone.name)

    @mock.patch("waldur_openstack.backend.logger")
    def test_not_use_default_volume_availability_zone_if_it_not_exists(
        self, mock_logger
    ):
        self.tenant.service_settings.options["volume_availability_zone_name"] = (
            "not_exists_volume_availability_zone"
        )
        self.tenant.service_settings.save()
        volume = self._get_volume()
        self.assertEqual(volume.availability_zone, None)
        mock_logger.error.assert_called_once()

    def test_intended_bootable_flag_survives_cinder_reporting_false_at_create(self):
        # Cinder reports bootable="false" right after create for an image-backed
        # volume until the image copy completes. The flag the serializer set on
        # a system volume must survive so create_instance can find it via
        # `volumes.get(bootable=True)` (regression for PUHURI-PORTALS-T2B).
        backend_volume = self._get_valid_volume("new-backend-id")
        backend_volume.bootable = "false"
        self.mocked_cinder.volumes.create.return_value = backend_volume

        volume = self._get_volume(bootable=True)

        self.assertTrue(volume.bootable)

    def test_bootable_flag_is_set_when_cinder_reports_true(self):
        # A volume created from an image that Cinder already reports as bootable
        # gets the flag even if it was not pre-set in the DB.
        backend_volume = self._get_valid_volume("new-backend-id")
        backend_volume.bootable = "true"
        self.mocked_cinder.volumes.create.return_value = backend_volume

        volume = self._get_volume(bootable=False)

        self.assertTrue(volume.bootable)

    def _get_volume(self, bootable=False):
        volume = factories.VolumeFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            backend_id=None,
            bootable=bootable,
        )

        backend = OpenStackBackend(self.openstack_settings)
        backend.create_volume(volume)
        volume.refresh_from_db()
        return volume


class ImportVolumeTest(BaseBackendTest):
    def setUp(self):
        super().setUp()
        self.backend_volume_id = "backend_id"
        self.backend_volume = self._get_valid_volume(self.backend_volume_id)

        self.mocked_cinder.volumes.get.return_value = self.backend_volume

    def test_volume_is_imported(self):
        volume = self.backend.import_volume(
            self.tenant, self.backend_volume_id, project=self.fixture.project, save=True
        )

        self.assertTrue(
            models.Volume.objects.filter(backend_id=self.backend_volume_id).exists()
        )
        self.assertEqual(
            str(models.Volume.objects.get(backend_id=self.backend_volume_id).uuid),
            str(volume.uuid),
        )
        self.assertEqual(volume.name, self.backend_volume.name)

    def test_volume_instance_is_not_created_during_import(self):
        vm = factories.InstanceFactory(
            backend_id="instance_backend_id",
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        self.backend_volume.attachments = [dict(server_id=vm.backend_id)]
        volume = self.backend.import_volume(
            self.tenant, self.backend_volume_id, project=self.fixture.project, save=True
        )

        self.assertIsNotNone(volume.instance)
        self.assertTrue(
            models.Volume.objects.filter(backend_id=self.backend_volume_id).exists()
        )
        self.assertEqual(
            str(models.Volume.objects.get(backend_id=self.backend_volume_id).uuid),
            str(volume.uuid),
        )
        self.assertEqual(volume.name, self.backend_volume.name)

    def test_import_instance_volumes_handles_missing_volumes(self):
        """Test that missing volumes are skipped gracefully during instance import."""
        from unittest.mock import patch

        # Set up mock to raise NotFound for missing volume
        self.mocked_cinder.volumes.get.side_effect = cinder_exceptions.NotFound(404)

        # Test _import_instance_volumes with a missing volume
        attached_volume_ids = [self.backend_volume_id, "another_volume_id"]

        with patch("waldur_openstack.backend.logger") as mock_logger:
            volumes = self.backend._import_instance_volumes(
                self.tenant,
                attached_volume_ids,
                project=self.fixture.project,
                save=True,
            )

            # Verify that warning was logged for missing volumes
            self.assertEqual(mock_logger.warning.call_count, 2)
            # Verify empty list is returned when all volumes are missing
            self.assertEqual(len(volumes), 0)

    def test_import_instance_volumes_with_mixed_existing_and_missing(self):
        """Test that existing volumes are kept while missing ones are skipped."""
        from unittest.mock import patch

        # Create an existing volume in the database
        factories.VolumeFactory(
            backend_id="existing_volume_id",
            tenant=self.tenant,
            project=self.fixture.project,
        )

        # Mock cinder to fail for new volume import
        self.mocked_cinder.volumes.get.side_effect = cinder_exceptions.NotFound(404)

        # Test with one existing and one missing volume
        attached_volume_ids = ["existing_volume_id", "missing_volume_id"]

        with patch("waldur_openstack.backend.logger") as mock_logger:
            volumes = self.backend._import_instance_volumes(
                self.tenant,
                attached_volume_ids,
                project=self.fixture.project,
                save=True,
            )

            # Verify that existing volume is returned
            self.assertEqual(len(volumes), 1)
            self.assertEqual(volumes[0].backend_id, "existing_volume_id")
            # Verify warning was logged for missing volume
            self.assertEqual(mock_logger.warning.call_count, 1)

    def test_import_volume_with_authentication_failure(self):
        """Test that authentication failures are properly handled and re-raised."""
        from unittest.mock import patch

        from waldur_openstack.exceptions import OpenStackAuthorizationFailed

        # Mock session creation to raise auth error
        with patch("waldur_openstack.backend.get_tenant_session") as mock_get_session:
            mock_get_session.side_effect = OpenStackAuthorizationFailed(
                "Invalid credentials"
            )

            with patch("waldur_openstack.backend.logger") as mock_logger:
                # Verify that auth error is raised
                with self.assertRaises(OpenStackAuthorizationFailed):
                    self.backend.import_volume(
                        self.tenant,
                        self.backend_volume_id,
                        project=self.fixture.project,
                        save=True,
                    )

                # Verify error was logged
                self.assertTrue(mock_logger.error.called)

    def test_import_instance_volumes_with_authentication_failure(self):
        """Test that authentication failures during volume import stop the process."""
        from unittest.mock import patch

        from waldur_openstack.exceptions import OpenStackAuthorizationFailed

        # Create scenario where volume doesn't exist locally
        attached_volume_ids = ["new_volume_id"]

        # Mock session creation to raise auth error when trying to import
        with patch("waldur_openstack.backend.get_tenant_session") as mock_get_session:
            mock_get_session.side_effect = OpenStackAuthorizationFailed(
                "Session expired"
            )

            with patch("waldur_openstack.backend.logger") as mock_logger:
                # Verify that auth error is raised and not caught
                with self.assertRaises(OpenStackAuthorizationFailed):
                    self.backend._import_instance_volumes(
                        self.tenant,
                        attached_volume_ids,
                        project=self.fixture.project,
                        save=True,
                    )

                # Verify error was logged
                self.assertTrue(mock_logger.error.called)


class PullVolumeTest(BaseBackendTest):
    def setUp(self):
        super().setUp()
        self.backend_volume_id = "backend_id"
        self.backend_volume = self._get_valid_volume(self.backend_volume_id)

        self.mocked_cinder.volumes.get.return_value = self.backend_volume

    def test_volume_instance_is_pulled(self):
        vm = factories.InstanceFactory(
            backend_id="instance_backend_id",
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        volume = factories.VolumeFactory(
            backend_id=self.backend_volume_id,
            instance=vm,
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        self.backend_volume.attachments = [dict(server_id=vm.backend_id)]
        self.backend.pull_volume(volume)
        volume.refresh_from_db()

        self.assertEqual(volume.instance, vm)

    def test_volume_image_is_pulled(self):
        volume = factories.VolumeFactory(
            backend_id=self.backend_volume_id,
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        image = ImageFactory(settings=self.fixture.tenant.service_settings)
        self.backend_volume.volume_image_metadata = {"image_id": image.backend_id}
        self.backend.pull_volume(volume)
        volume.refresh_from_db()

        self.assertEqual(volume.image, image)

    def test_volume_image_is_not_pulled(self):
        volume = factories.VolumeFactory(
            backend_id=self.backend_volume_id,
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        self.backend_volume.volume_image_metadata = {}
        self.backend.pull_volume(volume)
        volume.refresh_from_db()

        self.assertEqual(volume.image, None)


class PullInstanceAvailabilityZonesTest(BaseBackendTest):
    def test_default_zone_is_not_pulled(self):
        self.mocked_nova.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": "nova", "zoneState": {"available": True}})
        ]
        self.backend.pull_tenant_instance_availability_zones(self.tenant)
        self.assertEqual(models.InstanceAvailabilityZone.objects.count(), 0)

    def test_missing_zone_is_created(self):
        self.mocked_nova.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": "AZ_T1", "zoneState": {"available": True}})
        ]

        self.backend.pull_tenant_instance_availability_zones(self.tenant)
        self.assertEqual(models.InstanceAvailabilityZone.objects.count(), 1)

        zone = models.InstanceAvailabilityZone.objects.get()
        self.assertEqual(zone.name, "AZ_T1")
        self.assertTrue(zone.available)

    def test_stale_zone_is_removed(self):
        self.fixture.instance_availability_zone
        self.mocked_nova.availability_zones.list.return_value = []

        self.backend.pull_tenant_instance_availability_zones(self.tenant)
        self.assertEqual(models.InstanceAvailabilityZone.objects.count(), 0)

    def test_existing_zone_is_updated(self):
        zone = self.fixture.instance_availability_zone
        self.mocked_nova.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": zone.name, "zoneState": {"available": False}})
        ]

        self.backend.pull_tenant_instance_availability_zones(self.tenant)
        self.assertEqual(models.InstanceAvailabilityZone.objects.count(), 1)

        zone = models.InstanceAvailabilityZone.objects.get()
        self.assertFalse(zone.available)


class PullVolumeAvailabilityZonesTest(BaseBackendTest):
    def setUp(self):
        super().setUp()
        self.backend.is_volume_availability_zone_supported = lambda: True

    def test_default_zone_is_not_pulled(self):
        self.mocked_cinder.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": "nova", "zoneState": {"available": True}})
        ]
        self.backend.pull_tenant_volume_availability_zones(self.tenant)
        self.assertEqual(models.VolumeAvailabilityZone.objects.count(), 0)

    def test_missing_zone_is_created(self):
        self.mocked_cinder.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": "AZ_T1", "zoneState": {"available": True}})
        ]

        self.backend.pull_tenant_volume_availability_zones(self.tenant)
        self.assertEqual(models.VolumeAvailabilityZone.objects.count(), 1)

        zone = models.VolumeAvailabilityZone.objects.get()
        self.assertEqual(zone.name, "AZ_T1")
        self.assertTrue(zone.available)

    def test_stale_zone_is_removed(self):
        self.fixture.volume_availability_zone
        self.mocked_cinder.availability_zones.list.return_value = []

        self.backend.pull_tenant_volume_availability_zones(self.tenant)
        self.assertEqual(models.VolumeAvailabilityZone.objects.count(), 0)

    def test_existing_zone_is_updated(self):
        zone = self.fixture.volume_availability_zone
        self.mocked_cinder.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": zone.name, "zoneState": {"available": False}})
        ]

        self.backend.pull_tenant_volume_availability_zones(self.tenant)
        self.assertEqual(models.VolumeAvailabilityZone.objects.count(), 1)

        zone = models.VolumeAvailabilityZone.objects.get()
        self.assertFalse(zone.available)


class PullInstanceTest(BaseBackendTest):
    def setUp(self):
        super().setUp()

        class MockInstance:
            name = "instance_name"
            id = "instance_id"
            created = "2017-08-10"
            key_name = "key_name"
            flavor = {
                "vcpus": 1,
                "ram": 10240,
                "disk": 100,
                "ephemeral": 0,
                "swap": 0,
                "original_name": "flavor_name",
            }
            image = {"id": "image_id"}
            status = "ERRED"
            fault = {"message": "OpenStack Nova error."}
            networks = {
                "test-int-net": ["192.168.42.60"],
                "public": ["172.29.249.185"],
            }

            @classmethod
            def to_dict(cls):
                return {
                    "OS-EXT-AZ:availability_zone": "AZ_TST",
                    "OS-EXT-SRV-ATTR:hypervisor_hostname": "aio1.openstack.local",
                }

        self.mocked_nova.servers.get.return_value = MockInstance
        self.mocked_nova.volumes.get_server_volumes.return_value = []

    def test_availability_zone_is_pulled(self):
        zone = self.fixture.instance_availability_zone
        zone.name = "AZ_TST"
        zone.save()

        instance = self.fixture.instance

        self.backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.availability_zone, zone)

    def test_invalid_availability_zone_is_skipped(self):
        instance = self.fixture.instance

        self.backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.availability_zone, None)

    def test_error_message_is_synchronized(self):
        instance = self.fixture.instance

        self.backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.error_message, "OpenStack Nova error.")

    def test_existing_error_message_is_preserved_if_defined(self):
        del self.mocked_nova.servers.get.return_value.fault
        instance = self.fixture.instance
        instance.error_message = "Waldur error."
        instance.save()

        self.backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.error_message, "Waldur error.")

    def test_hypervisor_hostname_is_synchronized(self):
        instance = self.fixture.instance

        self.backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.hypervisor_hostname, "aio1.openstack.local")


class BackendInstanceToInstancePartialCellTest(BaseBackendTest):
    """Microversion 2.69 may return partial server entries when a cell is
    down: `created` becomes None and `status` becomes "UNKNOWN". The pull must
    not crash on these — produce a sane Instance instead so the rest of the
    sync continues."""

    def test_partial_cell_server_does_not_crash(self):
        partial_server = Server(
            manager=None,
            info={
                "id": "partial-id",
                "name": "partial",
                "status": "UNKNOWN",
                "key_name": "",
                "created": None,
                "OS-SRV-USG:launched_at": None,
                "flavor": None,
                "image": "",
                "networks": {},
            },
        )

        instance = self.backend._backend_instance_to_instance(
            self.tenant, partial_server
        )

        self.assertEqual(instance.backend_id, "partial-id")
        self.assertEqual(instance.runtime_state, "UNKNOWN")
        self.assertIsNone(instance.created)

    def test_partial_cell_server_with_missing_networks(self):
        partial_server = Server(
            manager=None,
            info={
                "id": "partial-id-2",
                "name": "partial-2",
                "status": "UNKNOWN",
                "key_name": "",
                "created": None,
                "image": "",
            },
        )

        instance = self.backend._backend_instance_to_instance(
            self.tenant, partial_server
        )

        self.assertEqual(instance.directly_connected_ips, "")


class PullInstancePortsTest(BaseBackendTest):
    def setup_neutron(self, port_id, device_id, subnet_id):
        self.mocked_neutron.list_ports.return_value = {
            "ports": [
                {
                    "id": port_id,
                    "mac_address": "DC-D6-5E-9B-49-70",
                    "device_id": device_id,
                    "device_owner": "compute:nova",
                    "admin_state_up": True,
                    "name": "port_1",
                    "description": "",
                    "fixed_ips": [
                        {
                            "ip_address": "10.0.0.2",
                            "subnet_id": subnet_id,
                        }
                    ],
                }
            ]
        }

    def test_pending_ports_are_updated_with_backend_id(self):
        # Arrange
        instance = self.fixture.instance
        port = self.fixture.port
        port.backend_id = ""
        port.save()
        self.setup_neutron("port_id", instance.backend_id, port.subnet.backend_id)

        # Act
        self.backend.pull_instance_ports(instance)

        # Assert
        port.refresh_from_db()
        self.assertEqual(port.backend_id, "port_id")

    def test_missing_ports_are_created(self):
        # Arrange
        instance = self.fixture.instance
        subnet = self.fixture.subnet
        self.setup_neutron("port_id", instance.backend_id, subnet.backend_id)

        # Act
        self.backend.pull_instance_ports(instance)

        # Assert
        self.assertEqual(instance.ports.count(), 1)
        port = instance.ports.first()
        self.assertEqual(port.backend_id, "port_id")
        self.assertEqual(port.subnet, subnet)

    def test_stale_ports_are_deleted(self):
        # Arrange
        instance = self.fixture.instance

        self.mocked_neutron.list_ports.return_value = {"ports": []}

        # Act
        self.backend.pull_instance_ports(instance)

        # Assert
        self.assertEqual(instance.ports.count(), 0)

    def test_stale_ports_are_deleted_by_backend_id(self):
        # Arrange
        vm = self.fixture.instance
        subnet = self.fixture.subnet

        PortFactory(
            subnet=self.fixture.subnet,
            instance=vm,
            tenant=self.fixture.tenant,
        )
        ip2 = PortFactory(
            subnet=self.fixture.subnet,
            instance=vm,
            tenant=self.fixture.tenant,
        )
        self.setup_neutron(ip2.backend_id, vm.backend_id, subnet.backend_id)

        # Act
        self.backend.pull_instance_ports(vm)

        # Assert
        self.assertEqual(vm.ports.count(), 1)
        self.assertEqual(vm.ports.first(), ip2)

    def test_existing_ports_are_updated(self):
        # Arrange
        instance = self.fixture.instance
        port = self.fixture.port
        self.setup_neutron(port.backend_id, instance.backend_id, port.subnet.backend_id)

        # Act
        self.backend.pull_instance_ports(instance)

        # Assert
        port.refresh_from_db()
        self.assertEqual(port.mac_address, "DC-D6-5E-9B-49-70")
        self.assertEqual(port.fixed_ips[0]["ip_address"], "10.0.0.2")

    def test_shared_ports_are_reassigned(self):
        # Arrange
        vm1 = factories.InstanceFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        vm2 = factories.InstanceFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )

        subnet = self.fixture.subnet
        port = PortFactory(
            subnet=self.fixture.subnet,
            instance=vm2,
            tenant=self.fixture.tenant,
        )
        self.setup_neutron(port.backend_id, vm1.backend_id, subnet.backend_id)

        # Act
        self.backend.pull_instance_ports(vm1)

        # Assert
        self.assertEqual(vm1.ports.count(), 1)
        self.assertEqual(vm2.ports.count(), 0)


@ddt
class PullPortsTest(BaseBackendTest):
    def setup_neutron(self, port_id, device_id, subnet_id):
        self.mocked_neutron.list_ports.return_value = {
            "ports": [
                {
                    "id": port_id,
                    "name": "",
                    "description": "",
                    "mac_address": "DC-D6-5E-9B-49-70",
                    "device_id": device_id,
                    "network_id": "network_id",
                    "device_owner": "compute:nova",
                    "admin_state_up": True,
                    "status": "ACTIVE",
                    "fixed_ips": [
                        {
                            "ip_address": "10.0.0.2",
                            "subnet_id": subnet_id,
                        }
                    ],
                    "security_groups": [],
                }
            ]
        }

    def test_missing_ports_are_created(self):
        # Arrange
        instance = self.fixture.instance
        subnet = self.fixture.subnet
        self.setup_neutron("port_id", instance.backend_id, subnet.backend_id)

        # Act
        self.backend.pull_tenant_ports(self.tenant)

        # Assert
        self.assertEqual(instance.ports.count(), 1)
        port = instance.ports.first()
        self.assertEqual(port.backend_id, "port_id")
        self.assertEqual(port.subnet, subnet)

    def test_stale_ports_are_deleted(self):
        # Arrange
        instance = self.fixture.instance

        self.mocked_neutron.list_ports.return_value = {"ports": []}

        # Act
        self.backend.pull_tenant_ports(self.tenant)

        # Assert
        self.assertEqual(instance.ports.count(), 0)

    @data(
        CoreStates.CREATION_SCHEDULED,
        CoreStates.CREATING,
        CoreStates.UPDATE_SCHEDULED,
        CoreStates.UPDATING,
        CoreStates.DELETION_SCHEDULED,
        CoreStates.DELETING,
    )
    def test_in_flight_ports_are_not_deleted(self, port_state):
        # Regression: OpenStackInstanceSerializer.create() saves Port rows in
        # CREATION_SCHEDULED state with backend_id=None, then create_instance_-
        # ports pushes them to Neutron and transitions them to OK. The
        # 2-hour periodic pull_tenant_ports must NOT delete these in-flight
        # ports — same goes for ports being updated or torn down.
        #
        # Original bug: pull_tenant_ports filtered only on tenant + backend_id,
        # so any in-flight port (state != OK/ERRED) whose backend_id wasn't yet
        # in Neutron's response — including the un-pushed NULL-backend_id port
        # from the create() flow — was wrongly deleted as "stale". Fixed by
        # adding state__in=[OK, ERRED] to the filter, matching every other
        # stale-detection site in this module.
        instance = self.fixture.instance
        in_flight_port = PortFactory(
            tenant=self.tenant,
            service_settings=self.openstack_settings,
            project=self.fixture.project,
            subnet=self.fixture.subnet,
            network=self.fixture.network,
            instance=instance,
            state=port_state,
            backend_id=None,
        )

        # Neutron returns no ports for this tenant — the in-flight Waldur
        # port has not yet been pushed, so Neutron doesn't know about it.
        self.mocked_neutron.list_ports.return_value = {"ports": []}

        self.backend.pull_tenant_ports(self.tenant)

        self.assertTrue(
            Port.objects.filter(pk=in_flight_port.pk).exists(),
            f"pull_tenant_ports must not delete ports in state {port_state} "
            "(only ports in OK or ERRED state should be considered stale)",
        )

    def test_existing_ports_are_updated(self):
        # Arrange
        instance = self.fixture.instance
        port = self.fixture.port
        self.setup_neutron(port.backend_id, instance.backend_id, port.subnet.backend_id)

        # Act
        self.backend.pull_tenant_ports(self.tenant)

        # Assert
        port.refresh_from_db()
        self.assertEqual(port.mac_address, "DC-D6-5E-9B-49-70")
        self.assertEqual(port.fixed_ips[0]["ip_address"], "10.0.0.2")

    def test_even_if_port_is_not_connected_it_is_not_skipped(self):
        # Arrange
        self.setup_neutron("port_id", "", self.fixture.port.subnet.backend_id)

        # Act
        self.backend.pull_tenant_ports(self.tenant)

        # Assert
        ports = Port.objects.filter(subnet=self.fixture.subnet)
        self.assertEqual(ports.count(), 1)

        port = ports.first()
        self.assertEqual(port.instance, None)
        self.assertEqual(port.backend_id, "port_id")
        self.assertEqual(port.mac_address, "DC-D6-5E-9B-49-70")
        self.assertEqual(port.fixed_ips[0]["ip_address"], "10.0.0.2")

    def test_port_subnet_is_none_if_fixed_ips_is_empty(self):
        # Arrange
        instance = self.fixture.instance
        port = self.fixture.port
        self.mocked_neutron.list_ports.return_value = {
            "ports": [
                {
                    "id": port.backend_id,
                    "name": "",
                    "description": "",
                    "mac_address": "DC-D6-5E-9B-49-70",
                    "device_id": instance.backend_id,
                    "network_id": "network_id",
                    "device_owner": "compute:nova",
                    "admin_state_up": True,
                    "status": "ACTIVE",
                    "fixed_ips": [],  # Empty fixed_ips array
                    "security_groups": [],
                }
            ]
        }

        # Act
        self.backend.pull_tenant_ports(self.tenant)

        # Assert
        port.refresh_from_db()
        self.assertIsNone(port.subnet)

    def test_port_subnet_is_none_if_subnet_id_not_found(self):
        # Arrange
        instance = self.fixture.instance
        port = self.fixture.port
        self.mocked_neutron.list_ports.return_value = {
            "ports": [
                {
                    "id": port.backend_id,
                    "name": "",
                    "description": "",
                    "mac_address": "DC-D6-5E-9B-49-70",
                    "device_id": instance.backend_id,
                    "network_id": "network_id",
                    "device_owner": "compute:nova",
                    "admin_state_up": True,
                    "status": "ACTIVE",
                    "fixed_ips": [
                        {
                            "ip_address": "10.0.0.2",
                            "subnet_id": "non_existing_subnet_id",  # Subnet ID that doesn't match any existing SubNet
                        }
                    ],
                    "security_groups": [],
                }
            ]
        }

        # Act
        self.backend.pull_tenant_ports(self.tenant)

        # Assert
        port.refresh_from_db()
        self.assertIsNone(port.subnet)

    def test_instance_has_several_ports_in_the_same_network_connected_to_the_same_instance(
        self,
    ):
        # Consider the case when instance has several IP addresses in the same subnet.

        # Arrange
        instance = self.fixture.instance
        subnet = self.fixture.subnet

        device_id = instance.backend_id
        subnet_id = subnet.backend_id

        self.mocked_neutron.list_ports.return_value = {
            "ports": [
                {
                    "id": "port1",
                    "mac_address": "fa:16:3e:88:d4:69",
                    "device_id": device_id,
                    "device_owner": "compute:nova",
                    "fixed_ips": [
                        {
                            "ip_address": "10.0.0.2",
                            "subnet_id": subnet_id,
                        }
                    ],
                    "name": "",
                    "description": "",
                    "admin_state_up": True,
                    "status": "ACTIVE",
                    "network_id": "network_id",
                    "security_groups": [],
                },
                {
                    "id": "port2",
                    "mac_address": "fa:16:3e:1f:fb:22",
                    "device_id": device_id,
                    "device_owner": "compute:nova",
                    "fixed_ips": [
                        {
                            "ip_address": "10.0.0.3",
                            "subnet_id": subnet_id,
                        }
                    ],
                    "name": "",
                    "description": "",
                    "admin_state_up": True,
                    "status": "ACTIVE",
                    "network_id": "network_id",
                    "security_groups": [],
                },
            ]
        }

        # Act
        self.backend.pull_tenant_ports(self.tenant)

        # Assert
        self.assertEqual(2, instance.ports.count())

        actual_subnets = set(instance.ports.values_list("subnet_id", flat=True))
        self.assertEqual({subnet.id}, actual_subnets)

        actual_addresses = list(instance.ports.values_list("fixed_ips", flat=True))
        self.assertEqual(
            [
                [{"ip_address": "10.0.0.3", "subnet_id": subnet_id}],
                [{"ip_address": "10.0.0.2", "subnet_id": subnet_id}],
            ],
            actual_addresses,
        )

        actual_ids = set(instance.ports.values_list("backend_id", flat=True))
        self.assertEqual({"port1", "port2"}, actual_ids)

    @data("compute:nova", "compute:MS-ZONE")
    def test_instance_field_of_port_is_updated(self, device_owner):
        # Consider the case when instance has several IP addresses in the same subnet.

        # Arrange
        instance = self.fixture.instance
        subnet = self.fixture.subnet

        port = self.fixture.port
        port.instance = None
        port.save()

        device_id = instance.backend_id
        subnet_id = subnet.backend_id

        self.mocked_neutron.list_ports.return_value = {
            "ports": [
                {
                    "id": port.backend_id,
                    "mac_address": "fa:16:3e:88:d4:69",
                    "device_id": device_id,
                    "device_owner": device_owner,
                    "fixed_ips": [
                        {
                            "ip_address": "10.0.0.2",
                            "subnet_id": subnet_id,
                        }
                    ],
                    "name": "",
                    "description": "",
                    "admin_state_up": True,
                    "status": "ACTIVE",
                    "network_id": "network_id",
                    "security_groups": [],
                }
            ]
        }

        # Act
        self.backend.pull_tenant_ports(self.tenant)

        # Assert
        port.refresh_from_db()
        self.assertEqual(port.instance, instance)
        self.assertEqual(1, instance.ports.count())


class GetInstancesTest(BaseBackendTest):
    def setUp(self):
        super().setUp()

    def _generate_instances(self, backend=False, count=1):
        instances = []
        for i in range(count):
            instance = factories.InstanceFactory()
            backend_instance = self._get_valid_instance(backend_id=instance.backend_id)
            if backend:
                instance.delete()
            instances.append(backend_instance)

        return instances

    def test_all_instances_returned(self):
        backend_instances = self._generate_instances(backend=True, count=3)
        instances = backend_instances + self._generate_instances()

        self.mocked_nova.servers.list.return_value = instances

        result = self.backend.get_instances(self.tenant)

        returned_backend_ids = [item.backend_id for item in result]
        expected_backend_ids = [item.id for item in instances]
        self.assertEqual(sorted(returned_backend_ids), sorted(expected_backend_ids))


class ImportInstanceTest(BaseBackendTest):
    def setUp(self):
        super().setUp()
        self.backend_id = "instance_id"
        self.backend_instance = self._get_valid_instance(self.backend_id)
        self.mocked_nova.servers.get.return_value = self.backend_instance

        backend_image = self._get_valid_image(self.backend_id)
        self.backend_instance.image = backend_image

    def test_backend_instance_without_volumes_is_imported(self):
        self.mocked_nova.volumes.get_server_volumes.return_value = []

        instance = self.backend.import_instance(
            self.tenant,
            self.backend_id,
            self.fixture.project,
        )

        self.assertEqual(instance.backend_id, self.backend_id)
        self.assertTrue(
            models.Instance.objects.filter(backend_id=self.backend_id).exists()
        )
        self.assertEqual(
            str(models.Instance.objects.get(backend_id=self.backend_id).uuid),
            str(instance.uuid),
        )
        self.assertEqual(instance.name, self.backend_instance.name)

    def test_volume_is_attached_to_imported_instance_if_they_are_registered(self):
        expected_volume = factories.VolumeFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        backend_volume = self._get_valid_volume(backend_id=expected_volume.backend_id)
        backend_volume.volumeId = backend_volume.id
        self.mocked_nova.volumes.get_server_volumes.return_value = [backend_volume]
        self.mocked_cinder.volumes.get.return_value = backend_volume

        instance = self.backend.import_instance(
            self.tenant,
            self.backend_id,
            self.fixture.project,
        )

        self.assertEqual(instance.backend_id, self.backend_id)
        self.assertEqual(models.Volume.objects.count(), 1)
        self.assertEqual(instance.volumes.count(), 1)
        actual_backend_ids = [v.backend_id for v in instance.volumes.all()]
        self.assertEqual([backend_volume.id], actual_backend_ids)

    def test_instance_is_imported_with_attached_volume(self):
        volume_backend_id = "volume_id"
        backend_volume = self._get_valid_volume(backend_id=volume_backend_id)
        backend_volume.volumeId = backend_volume.id
        self.mocked_nova.volumes.get_server_volumes.return_value = [backend_volume]
        self.mocked_cinder.volumes.get.return_value = backend_volume

        instance = self.backend.import_instance(
            self.tenant,
            self.backend_id,
            self.fixture.project,
        )

        self.assertEqual(instance.backend_id, self.backend_id)
        self.assertEqual(models.Volume.objects.count(), 1)
        self.assertEqual(instance.volumes.count(), 1)
        actual_backend_ids = [v.backend_id for v in instance.volumes.all()]
        self.assertEqual([backend_volume.id], actual_backend_ids)

    def test_instance_error_message_is_filled_if_fault_is_provided_by_backend(self):
        expected_error_message = "An error occurred displaying an error"
        self.backend_instance.fault = dict(message=expected_error_message)
        self.mocked_nova.volumes.get_server_volumes.return_value = []

        instance = self.backend.import_instance(
            self.tenant,
            self.backend_id,
            self.fixture.project,
        )

        self.assertEqual(instance.backend_id, self.backend_id)
        self.assertEqual(instance.error_message, expected_error_message)


class PullInstanceFloatingIpsTest(BaseBackendTest):
    def test_port_is_reassigned_for_floating_ip(self):
        # Arrange
        subnet = self.fixture.subnet
        instance = self.fixture.instance

        ip1 = PortFactory(
            tenant=self.fixture.tenant,
            subnet=subnet,
            backend_id="port_id1",
            fixed_ips=[{"ip_address": "192.168.42.42", "subnet_id": subnet.backend_id}],
        )

        ip2 = PortFactory(
            tenant=self.fixture.tenant,
            subnet=subnet,
            backend_id="port_id2",
            fixed_ips=[{"ip_address": "192.168.42.62", "subnet_id": subnet.backend_id}],
            instance=instance,
        )

        fip = FloatingIPFactory(tenant=self.fixture.tenant, port=ip1)

        floatingips = [
            {
                "floating_ip_address": fip.address,
                "floating_network_id": "new_backend_network_id",
                "status": "DOWN",
                "id": fip.backend_id,
                "port_id": ip2.backend_id,
            }
        ]
        self.mocked_neutron.list_floatingips.return_value = {"floatingips": floatingips}

        # Act
        self.backend.pull_instance_floating_ips(instance)

        # Assert
        self.assertEqual(1, instance.floating_ips.count())

        fip.refresh_from_db()
        self.assertEqual(ip2, fip.port)


class PushInstanceFloatingIpsTest(BaseBackendTest):
    # Regression: push_instance_floating_ips calls
    # update_floatingip(port_id=floating_ip.port.backend_id). If the port row
    # has no backend_id (port not pushed to Neutron yet, or its push failed),
    # Neutron silently disassociates the FIP and its status stays at DOWN.
    # The PollRuntimeStateTask scheduled after this step then retries for
    # ~100 minutes before failing with no actionable message. Fail fast with
    # a clear error instead.

    def _make_attached_fip(self):
        port = PortFactory(
            tenant=self.fixture.tenant,
            subnet=self.fixture.subnet,
            instance=self.fixture.instance,
        )
        fip = FloatingIPFactory(
            tenant=self.fixture.tenant,
            port=port,
        )
        return fip, port

    def test_unpushed_port_raises_clear_backend_error(self):
        fip, port = self._make_attached_fip()
        port.backend_id = ""
        port.save()

        with self.assertRaises(OpenStackBackendError) as ctx:
            self.backend.push_instance_floating_ips(self.fixture.instance)

        self.assertIn("empty backend_id", str(ctx.exception))
        self.assertIn(port.uuid.hex, str(ctx.exception))
        self.mocked_neutron.update_floatingip.assert_not_called()
        self.mocked_neutron.list_floatingips.assert_not_called()

    def test_uncreated_floating_ip_raises_clear_backend_error(self):
        fip, _port = self._make_attached_fip()
        fip.backend_id = ""
        fip.save()

        with self.assertRaises(OpenStackBackendError) as ctx:
            self.backend.push_instance_floating_ips(self.fixture.instance)

        self.assertIn("no backend_id", str(ctx.exception))
        self.assertIn("create_floating_ip", str(ctx.exception))
        self.mocked_neutron.update_floatingip.assert_not_called()
        self.mocked_neutron.list_floatingips.assert_not_called()

    def test_happy_path_associates_floating_ip(self):
        fip, port = self._make_attached_fip()
        self.mocked_neutron.list_floatingips.return_value = {"floatingips": []}

        self.backend.push_instance_floating_ips(self.fixture.instance)

        self.mocked_neutron.update_floatingip.assert_called_once_with(
            fip.backend_id,
            body={"floatingip": {"port_id": port.backend_id}},
        )


class PushInstanceFloatingIpsNotFoundTest(BaseBackendTest):
    # Regression for the rc.10 silent-skip path: prior to this fix,
    # push_instance_floating_ips caught neutron NotFound on update_floatingip
    # and just logged a warning, leaving the FIP unassociated. The downstream
    # PollRuntimeStateTask would then spin on runtime_state=DOWN for ~100 min
    # before failing with no actionable error. Surface a clear, fail-fast
    # OpenStackBackendError instead — Neutron NotFound here can mean either
    # the FIP or the port_id we passed in is not visible to this session.

    def test_notfound_on_update_floatingip_raises_clear_backend_error(self):
        port = PortFactory(
            tenant=self.fixture.tenant,
            subnet=self.fixture.subnet,
            instance=self.fixture.instance,
        )
        fip = FloatingIPFactory(
            tenant=self.fixture.tenant,
            port=port,
        )
        # Brand-new FIP not yet associated to any port in Neutron, so the
        # initial list_floatingips returns empty and the connect-new loop
        # calls update_floatingip — which we mock to raise NotFound.
        self.mocked_neutron.list_floatingips.return_value = {"floatingips": []}
        self.mocked_neutron.update_floatingip.side_effect = neutron_exceptions.NotFound(
            "Resource not found"
        )

        with self.assertRaises(OpenStackBackendError) as ctx:
            self.backend.push_instance_floating_ips(self.fixture.instance)

        msg = str(ctx.exception)
        # Names both sides — operator can see which is missing.
        self.assertIn(fip.backend_id, msg)
        self.assertIn(port.backend_id, msg)
        self.assertIn("NotFound", msg)


class CreateInstanceTest(VolumesBaseTest):
    def setUp(self):
        super().setUp()
        self.flavor_id = "small_flavor"
        backend_flavor = self._get_valid_flavor(self.flavor_id)
        self.mocked_nova.flavors.get.return_value = backend_flavor
        self.mocked_nova.servers.create.return_value.id = uuid.uuid4()
        # Nova microversion >= 2.36 requires at least one nic; the backend
        # builds nics from instance.ports, so each test needs a port.
        self.fixture.port

    def test_zone_name_is_passed_to_nova_client(self):
        # Arrange
        zone = self.fixture.instance_availability_zone
        vm = self.fixture.instance
        vm.availability_zone = zone
        vm.save()

        # Act
        self.backend.create_instance(vm, self.flavor_id)

        # Assert
        kwargs = self.mocked_nova.servers.create.mock_calls[0][2]
        self.assertEqual(kwargs["availability_zone"], zone.name)

    def test_default_zone_name_is_passed_to_nova_client(self):
        # Arrange
        self.openstack_settings.options["availability_zone"] = (
            "default_availability_zone"
        )

        # Act
        self.backend.create_instance(self.fixture.instance, self.flavor_id)

        # Assert
        kwargs = self.mocked_nova.servers.create.mock_calls[0][2]
        self.assertEqual(kwargs["availability_zone"], "default_availability_zone")

    def test_scheduler_hints_use_server_group_when_backend_id_present(self):
        # Act
        self.backend.create_instance(
            self.fixture.instance, self.flavor_id, server_group="sg-backend-id"
        )

        # Assert
        kwargs = self.mocked_nova.servers.create.mock_calls[0][2]
        self.assertEqual(kwargs["scheduler_hints"], {"group": "sg-backend-id"})

    def test_scheduler_hints_omitted_when_server_group_backend_id_is_empty(self):
        # Regression: an empty server_group string used to forward
        # scheduler_hints={"group": ""} to Nova, which rejects it
        # with "'' is not a 'uuid'".
        self.backend.create_instance(
            self.fixture.instance, self.flavor_id, server_group=""
        )

        kwargs = self.mocked_nova.servers.create.mock_calls[0][2]
        self.assertNotIn("scheduler_hints", kwargs)

    def test_empty_nics_raises_clear_backend_error(self):
        # Regression: Nova microversion 2.36+ rejects an empty `nics` list with
        # a bare ValueError that escapes the ClientException handler. The
        # backend should raise OpenStackBackendError with an actionable message.
        instance = self.fixture.instance
        instance.ports.all().delete()

        with self.assertRaises(OpenStackBackendError) as ctx:
            self.backend.create_instance(instance, self.flavor_id)

        self.assertIn("at least one network port is required", str(ctx.exception))
        self.mocked_nova.servers.create.assert_not_called()

    def test_ports_without_backend_id_raise_clear_backend_error(self):
        # Regression: if port creation failed earlier and ports have empty
        # backend_id, nics ends up empty and Nova rejects the call. Surface a
        # clearer error pointing to the offending ports.
        instance = self.fixture.instance
        port = self.fixture.port
        port.backend_id = ""
        port.save()

        with self.assertRaises(OpenStackBackendError) as ctx:
            self.backend.create_instance(instance, self.flavor_id)

        self.assertIn("port creation likely failed earlier", str(ctx.exception))
        self.mocked_nova.servers.create.assert_not_called()

    def test_config_drive_per_instance_true_overrides_tenant_false(self):
        # Per-instance True must win over tenant-wide False.
        self.openstack_settings.options["config_drive"] = False
        instance = self.fixture.instance
        instance.config_drive = True
        instance.save()

        self.backend.create_instance(instance, self.flavor_id)

        kwargs = self.mocked_nova.servers.create.mock_calls[0][2]
        self.assertIs(kwargs["config_drive"], True)

    def test_config_drive_per_instance_false_overrides_tenant_true(self):
        # Per-instance False must win over tenant-wide True. Key absent from
        # kwargs preserves the existing behaviour of only setting the flag
        # when it is truthy.
        self.openstack_settings.options["config_drive"] = True
        instance = self.fixture.instance
        instance.config_drive = False
        instance.save()

        self.backend.create_instance(instance, self.flavor_id)

        kwargs = self.mocked_nova.servers.create.mock_calls[0][2]
        self.assertNotIn("config_drive", kwargs)

    def test_config_drive_falls_back_to_tenant_default_when_null(self):
        # config_drive=None on the instance → use the tenant-wide setting.
        self.openstack_settings.options["config_drive"] = True
        instance = self.fixture.instance
        instance.config_drive = None
        instance.save()

        self.backend.create_instance(instance, self.flavor_id)

        kwargs = self.mocked_nova.servers.create.mock_calls[0][2]
        self.assertIs(kwargs["config_drive"], True)


class CreateServerGroupTest(BaseBackendTest):
    def test_server_group_is_created_with_policy_kwarg(self):
        # Regression: novaclient microversion 2.64+ replaced the list-typed
        # `policies` kwarg with a single-string `policy`. Passing `policies`
        # raises TypeError("ServerGroupsManager.create() got an unexpected
        # keyword argument 'policies'").
        server_group = self.fixture.server_group
        server_group.backend_id = ""
        server_group.save()
        self.mocked_nova.server_groups.create.return_value.id = "sg-backend-id"

        self.backend.create_server_group(server_group)

        self.mocked_nova.server_groups.create.assert_called_once_with(
            name=server_group.name, policy=server_group.policy
        )
        server_group.refresh_from_db()
        self.assertEqual(server_group.backend_id, "sg-backend-id")


class EnhancedImageDetectionTest(BaseBackendTest):
    def setUp(self):
        super().setUp()
        self.backend_id = "test_instance_id"
        # Create instance with NO image metadata (empty image field)
        self.backend_instance_no_image = Server(
            manager=None,
            info={
                "id": self.backend_id,
                "name": "instance-no-image",
                "status": "ACTIVE",
                "key_name": "",
                "created": "2012-04-23T08:10:00Z",
                "OS-SRV-USG:launched_at": "2012-04-23T09:15",
                "flavor": {
                    "vcpus": 2,
                    "ram": 4096,
                    "disk": 10,
                    "ephemeral": 0,
                    "swap": 0,
                    "original_name": "m1.small",
                },
                "image": "",  # No image metadata
                "OS-EXT-SRV-ATTR:root_device_name": "/dev/vda",
                "networks": {"test-int-net": ["192.168.42.60"]},
            },
        )

        # Create a bootable volume with image metadata
        self.volume_backend_id = "bootable_volume_id"
        self.image_id_in_volume = "image_id_from_volume"
        self.image_name_in_volume = "Ubuntu 22.04 x86_64"

        self.bootable_volume = Volume(
            manager=None,
            info={
                "id": self.volume_backend_id,
                "name": "bootable-volume",
                "size": 20,
                "status": "in-use",
                "bootable": "true",
                "volume_image_metadata": {
                    "image_id": self.image_id_in_volume,
                    "image_name": self.image_name_in_volume,
                    "checksum": "b1baedc2f98d667e7f587692464b61d0",
                    "container_format": "bare",
                    "disk_format": "raw",
                    "min_disk": "10",
                    "min_ram": "1024",
                    "size": "2361393152",
                },
                "attachments": [
                    {
                        "id": self.volume_backend_id,
                        "volume_id": self.volume_backend_id,
                        "server_id": self.backend_id,
                        "device": "/dev/vda",
                    }
                ],
            },
        )

        # Create volume reference object for nova API
        self.volume_ref = type("VolumeRef", (), {"volumeId": self.volume_backend_id})

        # Flavor is embedded in server response with microversion 2.47+

    def test_image_detection_from_bootable_volume_with_image_id(self):
        """Test that image is detected from bootable volume when instance has no image metadata"""
        # Setup mocks
        self.mocked_nova.servers.get.return_value = self.backend_instance_no_image
        self.mocked_nova.volumes.get_server_volumes.return_value = [self.volume_ref]
        self.mocked_cinder.volumes.get.return_value = self.bootable_volume

        # Create the Image object in Waldur database that corresponds to the volume's image_id
        ImageFactory(
            settings=self.openstack_settings,
            backend_id=self.image_id_in_volume,
            name=self.image_name_in_volume,
        )

        # Create Volume object in Waldur database with image metadata
        factories.VolumeFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            backend_id=self.volume_backend_id,
            bootable=True,
            image_metadata={
                "image_id": self.image_id_in_volume,
                "image_name": self.image_name_in_volume,
            },
        )

        # Act
        instance = self.backend.import_instance(
            self.tenant, self.backend_id, self.fixture.project
        )

        # Assert
        self.assertEqual(instance.backend_id, self.backend_id)
        self.assertEqual(instance.image_name, self.image_name_in_volume)
        self.assertTrue(
            models.Instance.objects.filter(backend_id=self.backend_id).exists()
        )

    def test_image_name_fallback_when_image_id_not_in_waldur(self):
        """Test fallback to image_name when image_id from volume doesn't exist in Waldur"""
        # Setup mocks
        self.mocked_nova.servers.get.return_value = self.backend_instance_no_image
        self.mocked_nova.volumes.get_server_volumes.return_value = [self.volume_ref]
        self.mocked_cinder.volumes.get.return_value = self.bootable_volume

        # Create Image by name only (image_id from volume doesn't exist in Waldur)
        ImageFactory(
            settings=self.openstack_settings,
            backend_id="different_image_id",  # Different from volume's image_id
            name=self.image_name_in_volume,  # But same name
        )

        # Create Volume object in Waldur database with image metadata
        factories.VolumeFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            backend_id=self.volume_backend_id,
            bootable=True,
            image_metadata={
                "image_id": self.image_id_in_volume,  # This ID doesn't exist in Waldur
                "image_name": self.image_name_in_volume,  # But name does
            },
        )

        # Act
        instance = self.backend.import_instance(
            self.tenant, self.backend_id, self.fixture.project
        )

        # Assert - should use image_name directly
        self.assertEqual(instance.image_name, self.image_name_in_volume)

    def test_volume_prioritization_root_device_first(self):
        """Test that root device volume is prioritized over other bootable volumes"""
        # Create second bootable volume (not root device)
        secondary_volume_id = "secondary_volume_id"
        secondary_image_name = "Secondary Image"

        secondary_volume = Volume(
            manager=None,
            info={
                "id": secondary_volume_id,
                "name": "secondary-bootable-volume",
                "size": 10,
                "status": "in-use",
                "bootable": "true",
                "volume_image_metadata": {
                    "image_id": "secondary_image_id",
                    "image_name": secondary_image_name,
                },
                "attachments": [
                    {
                        "id": secondary_volume_id,
                        "volume_id": secondary_volume_id,
                        "server_id": self.backend_id,
                        "device": "/dev/vdb",  # Not root device
                    }
                ],
            },
        )

        secondary_volume_ref = type("VolumeRef", (), {"volumeId": secondary_volume_id})

        # Setup mocks - return secondary volume first, then root volume
        self.mocked_nova.servers.get.return_value = self.backend_instance_no_image
        self.mocked_nova.volumes.get_server_volumes.return_value = [
            secondary_volume_ref,
            self.volume_ref,
        ]

        def get_volume_side_effect(volume_id):
            if volume_id == secondary_volume_id:
                return secondary_volume
            elif volume_id == self.volume_backend_id:
                return self.bootable_volume

        self.mocked_cinder.volumes.get.side_effect = get_volume_side_effect

        # Create Volume objects in Waldur database
        factories.VolumeFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            backend_id=self.volume_backend_id,
            bootable=True,
            device="/dev/vda",  # Root device
            image_metadata={
                "image_id": self.image_id_in_volume,
                "image_name": self.image_name_in_volume,
            },
        )

        factories.VolumeFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            backend_id=secondary_volume_id,
            bootable=True,
            device="/dev/vdb",  # Not root device
            image_metadata={
                "image_id": "secondary_image_id",
                "image_name": secondary_image_name,
            },
        )

        # Act
        instance = self.backend.import_instance(
            self.tenant, self.backend_id, self.fixture.project
        )

        # Assert - should use root device volume image name, not secondary volume
        self.assertEqual(instance.image_name, self.image_name_in_volume)
        self.assertNotEqual(instance.image_name, secondary_image_name)

    def test_no_bootable_volumes_available(self):
        """Test that import works gracefully when no bootable volumes have image metadata"""
        # Setup mocks
        non_bootable_volume = Volume(
            manager=None,
            info={
                "id": "non_bootable_volume_id",
                "name": "data-volume",
                "size": 10,
                "status": "in-use",
                "bootable": "false",  # Not bootable
                "attachments": [
                    {
                        "id": "non_bootable_volume_id",
                        "volume_id": "non_bootable_volume_id",
                        "server_id": self.backend_id,
                        "device": "/dev/vdb",
                    }
                ],
            },
        )

        volume_ref = type("VolumeRef", (), {"volumeId": "non_bootable_volume_id"})

        self.mocked_nova.servers.get.return_value = self.backend_instance_no_image
        self.mocked_nova.volumes.get_server_volumes.return_value = [volume_ref]
        self.mocked_cinder.volumes.get.return_value = non_bootable_volume

        # Create non-bootable volume in Waldur
        factories.VolumeFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            backend_id="non_bootable_volume_id",
            bootable=False,
        )

        # Act
        instance = self.backend.import_instance(
            self.tenant, self.backend_id, self.fixture.project
        )

        # Assert - should import successfully without image name
        self.assertEqual(instance.backend_id, self.backend_id)
        self.assertEqual(instance.image_name, "")  # No image name available

    def test_image_detection_falls_back_to_volume_image_fk(self):
        """Test that image is detected from volume.image FK when image_metadata is empty"""
        self.mocked_nova.servers.get.return_value = self.backend_instance_no_image
        self.mocked_nova.volumes.get_server_volumes.return_value = [self.volume_ref]
        self.mocked_cinder.volumes.get.return_value = self.bootable_volume

        # Create Image in Waldur database
        image = ImageFactory(
            settings=self.openstack_settings,
            backend_id=self.image_id_in_volume,
            name=self.image_name_in_volume,
        )

        # Create Volume with empty image_metadata but with image FK set
        factories.VolumeFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            backend_id=self.volume_backend_id,
            bootable=True,
            device="/dev/vda",
            image_metadata="",
            image=image,
        )

        # Act
        instance = self.backend.import_instance(
            self.tenant, self.backend_id, self.fixture.project
        )

        # Assert - should resolve image name via volume.image FK
        self.assertEqual(instance.image_name, self.image_name_in_volume)

    def test_pull_tenant_instances_uses_enhanced_detection(self):
        """Test that pull_tenant_instances now uses enhanced image detection via pull_instance"""
        # Create instance in Waldur database
        instance = factories.InstanceFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            backend_id=self.backend_id,
            state=CoreStates.OK,  # Ensure instance is in OK state so it gets processed
        )

        # Make instance appear older to bypass pull_instance timing check
        instance.modified = timezone.now() - datetime.timedelta(seconds=1)
        instance.save(update_fields=["modified"])

        # Setup mocks for pull_instance path
        self.mocked_nova.servers.get.return_value = self.backend_instance_no_image
        self.mocked_nova.volumes.get_server_volumes.return_value = [self.volume_ref]
        self.mocked_cinder.volumes.get.return_value = self.bootable_volume

        # Create Volume object in Waldur database with image metadata
        factories.VolumeFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            backend_id=self.volume_backend_id,
            bootable=True,
            image_metadata={
                "image_id": self.image_id_in_volume,
                "image_name": self.image_name_in_volume,
            },
        )

        # Mock ports query (required by pull_instance)
        with mock.patch.object(instance.ports, "all") as mock_ports_all:
            mock_ports_all.return_value.values_list.return_value = []

            # Act
            with mock.patch.object(self.backend, "pull_instance_security_groups"):
                self.backend.pull_tenant_instances(self.tenant)

        # Assert - instance should have been updated with image name from bootable volume
        instance.refresh_from_db()
        self.assertEqual(instance.image_name, self.image_name_in_volume)

    def test_pull_tenant_instances_handles_not_found_instance(self):
        from novaclient import exceptions as nova_exceptions

        instance = factories.InstanceFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            backend_id="NONEXISTENT_ID",
            state=CoreStates.OK,
        )

        self.mocked_nova.servers.get.side_effect = nova_exceptions.NotFound(code=404)

        with mock.patch.object(instance.ports, "all") as mock_ports_all:
            mock_ports_all.return_value.values_list.return_value = []
            self.backend.pull_tenant_instances(self.tenant)

        instance.refresh_from_db()
        self.assertEqual(instance.state, CoreStates.ERRED)
        self.assertIn("Does not exist at backend", instance.error_message)


class GetConsoleUrlDomainOverrideTest(BaseBackendTest):
    def setUp(self):
        super().setUp()
        self.instance = self.fixture.instance
        self.original_url = (
            "http://nova-console.internal:13080/vnc_auto.html?token=abc123"
        )

    def _get_console_url(self, override_value):
        self.openstack_settings.options["console_domain_override"] = override_value
        self.openstack_settings.save()
        self.mocked_nova.servers.get_console_url.return_value = {
            "console": {"url": self.original_url}
        }
        return self.backend.get_console_url(self.instance)

    def test_domain_only_override_preserves_original_port(self):
        url = self._get_console_url("lb.example.com")
        self.assertEqual(url, "http://lb.example.com:13080/vnc_auto.html?token=abc123")

    def test_domain_and_port_override_replaces_both(self):
        url = self._get_console_url("lb.example.com:443")
        self.assertEqual(url, "http://lb.example.com:443/vnc_auto.html?token=abc123")

    def test_domain_override_without_original_port(self):
        self.original_url = "http://nova-console.internal/vnc_auto.html?token=abc123"
        url = self._get_console_url("lb.example.com")
        self.assertEqual(url, "http://lb.example.com/vnc_auto.html?token=abc123")

    def test_domain_and_port_override_without_original_port(self):
        self.original_url = "http://nova-console.internal/vnc_auto.html?token=abc123"
        url = self._get_console_url("lb.example.com:443")
        self.assertEqual(url, "http://lb.example.com:443/vnc_auto.html?token=abc123")

    def test_no_override_returns_original_url(self):
        self.mocked_nova.servers.get_console_url.return_value = {
            "console": {"url": self.original_url}
        }
        url = self.backend.get_console_url(self.instance)
        self.assertEqual(url, self.original_url)


class RescueBackendTest(BaseBackendTest):
    """Lock in the novaclient calling convention for rescue / unrescue.

    Lab validation caught a real bug where backend was passing image_ref=
    instead of image= — novaclient's servers.rescue() takes image=
    (and maps it to rescue_image_ref in the wire request body).
    """

    def setUp(self):
        super().setUp()
        self.instance = self.fixture.instance

    def test_rescue_uses_image_kwarg_not_image_ref(self):
        # Regression guard: novaclient's servers.rescue() takes `image=`,
        # NOT `image_ref=`. Calling with image_ref= raises TypeError at
        # runtime — verified against the lab cloud.
        self.backend.rescue_instance(
            self.instance, rescue_image_ref="rescue-image-uuid"
        )
        call_kwargs = self.mocked_nova.servers.rescue.call_args.kwargs
        self.assertIn("image", call_kwargs)
        self.assertNotIn("image_ref", call_kwargs)
        self.assertEqual(call_kwargs["image"], "rescue-image-uuid")

    def test_rescue_passes_none_when_no_image_provided(self):
        self.backend.rescue_instance(self.instance)
        call_kwargs = self.mocked_nova.servers.rescue.call_args.kwargs
        self.assertIsNone(call_kwargs["image"])

    def test_rescue_409_already_rescued_is_idempotent(self):
        self.mocked_nova.servers.rescue.side_effect = nova_exceptions.ClientException(
            code=409, message="Cannot rescue while in vm_state rescued"
        )
        # Should NOT raise.
        self.backend.rescue_instance(self.instance, rescue_image_ref="x")

    def test_unrescue_409_already_active_is_idempotent(self):
        self.mocked_nova.servers.unrescue.side_effect = nova_exceptions.ClientException(
            code=409, message="Cannot unrescue while in vm_state active"
        )
        # Should NOT raise.
        self.backend.unrescue_instance(self.instance)


class PushTenantQuotasTest(BaseBackendTest):
    """Verify that push_tenant_quotas maps Waldur quota names to the correct
    neutron/nova/cinder API keys and passes them to the right client calls."""

    def _push(self, quotas):
        self.backend.push_tenant_quotas(self.tenant, quotas)

    def test_security_group_quotas_map_to_neutron(self):
        self._push({"security_group_count": 10, "security_group_rule_count": 20})
        self.mocked_neutron.update_quota.assert_called_once_with(
            self.tenant.backend_id,
            {"quota": {"security_group": 10, "security_group_rule": 20}},
        )

    def test_floating_ip_count_maps_to_floatingip(self):
        self._push({"floating_ip_count": 5})
        call_args = self.mocked_neutron.update_quota.call_args
        quota_body = call_args[0][1]["quota"]
        self.assertEqual(quota_body["floatingip"], 5)
        self.assertNotIn("floating_ip_count", quota_body)

    def test_network_count_maps_to_network(self):
        self._push({"network_count": 3})
        quota_body = self.mocked_neutron.update_quota.call_args[0][1]["quota"]
        self.assertEqual(quota_body["network"], 3)
        self.assertNotIn("network_count", quota_body)

    def test_subnet_count_maps_to_subnet(self):
        self._push({"subnet_count": 15})
        quota_body = self.mocked_neutron.update_quota.call_args[0][1]["quota"]
        self.assertEqual(quota_body["subnet"], 15)
        self.assertNotIn("subnet_count", quota_body)

    def test_port_count_maps_to_port(self):
        self._push({"port_count": 50})
        quota_body = self.mocked_neutron.update_quota.call_args[0][1]["quota"]
        self.assertEqual(quota_body["port"], 50)
        self.assertNotIn("port_count", quota_body)

    def test_neutron_quotas_accept_zero(self):
        self._push({"floating_ip_count": 0})
        quota_body = self.mocked_neutron.update_quota.call_args[0][1]["quota"]
        self.assertEqual(quota_body["floatingip"], 0)

    def test_neutron_quotas_accept_unlimited(self):
        self._push({"floating_ip_count": -1})
        quota_body = self.mocked_neutron.update_quota.call_args[0][1]["quota"]
        self.assertEqual(quota_body["floatingip"], -1)

    def test_all_four_neutron_quotas_sent_together(self):
        self._push(
            {
                "floating_ip_count": 10,
                "network_count": 5,
                "subnet_count": 20,
                "port_count": 100,
            }
        )
        quota_body = self.mocked_neutron.update_quota.call_args[0][1]["quota"]
        self.assertEqual(quota_body["floatingip"], 10)
        self.assertEqual(quota_body["network"], 5)
        self.assertEqual(quota_body["subnet"], 20)
        self.assertEqual(quota_body["port"], 100)

    def test_omitted_neutron_quotas_not_sent(self):
        # Only floating_ip_count provided — other neutron keys must be absent.
        self._push({"floating_ip_count": 5})
        quota_body = self.mocked_neutron.update_quota.call_args[0][1]["quota"]
        self.assertNotIn("network", quota_body)
        self.assertNotIn("subnet", quota_body)
        self.assertNotIn("port", quota_body)

    def test_neutron_call_skipped_when_no_neutron_quotas(self):
        # Nova-only quotas must not trigger a neutron update_quota call.
        self._push({"instances": 10, "vcpu": 4, "ram": 8192})
        self.mocked_neutron.update_quota.assert_not_called()

    def test_volume_type_quota_forwarded_to_cinder(self):
        self._push({"gigabytes_ssd": 500})
        self.mocked_cinder.quotas.update.assert_called_once_with(
            self.tenant.backend_id, gigabytes_ssd=500
        )

    def test_multiple_volume_type_quotas_merged_with_cinder_quotas(self):
        # storage (MiB) is converted to GB for Cinder; gigabytes_* are passed as-is (GB).
        self._push(
            {"storage": 1024, "gigabytes_ssd": 200, "gigabytes___DEFAULT__": 400}
        )
        call_kwargs = self.mocked_cinder.quotas.update.call_args[1]
        self.assertEqual(call_kwargs["gigabytes"], 1)
        self.assertEqual(call_kwargs["gigabytes_ssd"], 200)
        self.assertEqual(call_kwargs["gigabytes___DEFAULT__"], 400)

    def test_volume_type_quota_unlimited_value_forwarded(self):
        self._push({"gigabytes_ssd": -1})
        call_kwargs = self.mocked_cinder.quotas.update.call_args[1]
        self.assertEqual(call_kwargs["gigabytes_ssd"], -1)

    def test_volume_type_quota_zero_value_forwarded(self):
        self._push({"gigabytes_ssd": 0})
        call_kwargs = self.mocked_cinder.quotas.update.call_args[1]
        self.assertEqual(call_kwargs["gigabytes_ssd"], 0)

    def test_nova_not_called_when_only_volume_type_quotas(self):
        self._push({"gigabytes_ssd": 100})
        self.mocked_nova.quotas.update.assert_not_called()


class PushInstancePortsTest(BaseBackendTest):
    def _prepare_new_port(self):
        instance = self.fixture.instance
        port = self.fixture.port
        # A port pulled from the backend carries the concrete fixed IP but has
        # not yet been (re)created in Neutron.
        port.backend_id = ""
        port.fixed_ips = [
            {"subnet_id": self.fixture.subnet.backend_id, "ip_address": "10.0.0.5"}
        ]
        port.save()
        return instance, port

    def test_new_port_is_created_via_admin_session(self):
        instance, port = self._prepare_new_port()

        tenant_neutron = mock.Mock()
        tenant_neutron.list_ports.return_value = {"ports": []}
        admin_neutron = mock.Mock()
        admin_neutron.create_port.return_value = {
            "port": {
                "id": "created-port-id",
                "mac_address": "fa:16:3e:00:00:01",
                "fixed_ips": port.fixed_ips,
            }
        }

        def fake_get_neutron_client(session):
            return admin_neutron if session == "ADMIN" else tenant_neutron

        with (
            mock.patch(
                "waldur_openstack.backend.get_tenant_session", return_value="TENANT"
            ),
            mock.patch.object(
                OpenStackBackend,
                "admin_session",
                new_callable=mock.PropertyMock,
                return_value="ADMIN",
            ),
            mock.patch(
                "waldur_openstack.backend.get_neutron_client",
                side_effect=fake_get_neutron_client,
            ),
            mock.patch("waldur_openstack.backend.get_nova_client") as mock_nova,
        ):
            self.backend.push_instance_ports(instance)

        # Specifying an explicit fixed ip_address on create is admin-only in
        # Neutron, so the port must be created through the admin session and
        # never the tenant one.
        admin_neutron.create_port.assert_called_once()
        tenant_neutron.create_port.assert_not_called()

        payload = admin_neutron.create_port.call_args[0][0]["port"]
        self.assertEqual(payload["fixed_ips"], port.fixed_ips)
        # An admin-created port without explicit ownership lands in the admin
        # project and the tenant-scoped interface_attach 404s on it.
        self.assertEqual(payload["tenant_id"], port.tenant.backend_id)
        self.assertEqual(payload["project_id"], port.tenant.backend_id)

        mock_nova.return_value.servers.interface_attach.assert_called_once()
        port.refresh_from_db()
        self.assertEqual(port.backend_id, "created-port-id")
