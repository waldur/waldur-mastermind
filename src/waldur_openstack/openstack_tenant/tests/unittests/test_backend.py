import uuid
from unittest import mock

from cinderclient.v2.volumes import Volume
from ddt import data, ddt
from django.test import TestCase
from novaclient.v2.flavors import Flavor
from novaclient.v2.servers import Server

from waldur_openstack.openstack.backend import OpenStackBackend
from waldur_openstack.openstack.models import Port
from waldur_openstack.openstack.tests.factories import FloatingIPFactory, PortFactory
from waldur_openstack.openstack_base.tests.fixtures import mock_session
from waldur_openstack.openstack_tenant import models
from waldur_openstack.openstack_tenant.backend import OpenStackTenantBackend

from .. import factories, fixtures


class BaseBackendTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.settings = self.fixture.openstack_tenant_service_settings
        self.tenant = self.fixture.tenant
        self.mocked_neutron = mock.patch("neutronclient.v2_0.client.Client").start()()
        self.mocked_cinder = mock.patch("cinderclient.v3.client.Client").start()()
        self.mocked_nova = mock.patch("novaclient.v2.client.Client").start()()
        self.tenant_backend = OpenStackTenantBackend(self.settings)
        self.os_backend = OpenStackBackend(
            self.tenant.service_settings, self.tenant.backend_id
        )
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
                "flavor": {"id": backend_id},
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

        result = self.tenant_backend.get_volumes()

        returned_backend_ids = [item.backend_id for item in result]
        expected_backend_ids = [item.id for item in volumes]
        self.assertEqual(sorted(returned_backend_ids), sorted(expected_backend_ids))


class CreateVolumesTest(VolumesBaseTest):
    def setUp(self):
        super().setUp()
        self.mocked_cinder.volumes.create.return_value = self._generate_volumes()[0]

    def test_use_default_volume_type_if_type_not_populated(self):
        volume_type = factories.VolumeTypeFactory(settings=self.settings)
        self.tenant.default_volume_type_name = volume_type.name
        self.tenant.save()
        volume = self._get_volume()
        self.assertEqual(volume.type.name, volume_type.name)

    def test_do_not_use_volume_type_if_settings_have_no_scope(self):
        self.settings.scope = None
        self.settings.save()
        volume = self._get_volume()
        self.assertEqual(volume.type, None)

    @mock.patch("waldur_openstack.openstack_tenant.backend.logger")
    def test_not_use_default_volume_type_if_it_not_exists(self, mock_logger):
        self.tenant.default_volume_type_name = "not_exists_value_type"
        self.tenant.save()
        volume = self._get_volume()
        self.assertEqual(volume.type, None)
        mock_logger.error.assert_called_once()

    @mock.patch("waldur_openstack.openstack_tenant.backend.logger")
    def test_not_use_default_volume_type_if_two_types_exist(self, mock_logger):
        volume_type = factories.VolumeTypeFactory(settings=self.settings)
        factories.VolumeTypeFactory(name=volume_type.name, settings=self.settings)
        self.tenant.default_volume_type_name = volume_type.name
        self.tenant.save()
        volume = self._get_volume()
        self.assertEqual(volume.type, None)
        mock_logger.error.assert_called_once()

    def test_use_default_volume_availability_zone_if_zone_not_populated(self):
        volume_availability_zone = factories.VolumeAvailabilityZoneFactory(
            settings=self.settings
        )
        self.tenant.service_settings.options["volume_availability_zone_name"] = (
            volume_availability_zone.name
        )
        self.tenant.service_settings.save()
        volume = self._get_volume()
        self.assertEqual(volume.availability_zone.name, volume_availability_zone.name)

    def test_do_not_use_volume_availability_zone_if_settings_have_no_scope(self):
        self.settings.scope = None
        self.settings.save()
        volume = self._get_volume()
        self.assertEqual(volume.availability_zone, None)

    @mock.patch("waldur_openstack.openstack_tenant.backend.logger")
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

    def _get_volume(self):
        volume = factories.VolumeFactory(
            service_settings=self.fixture.openstack_tenant_service_settings,
            project=self.fixture.project,
            backend_id=None,
        )

        backend = OpenStackTenantBackend(self.settings)
        backend.create_volume(volume)
        return volume


class ImportVolumeTest(BaseBackendTest):
    def setUp(self):
        super().setUp()
        self.backend_volume_id = "backend_id"
        self.backend_volume = self._get_valid_volume(self.backend_volume_id)

        self.mocked_cinder.volumes.get.return_value = self.backend_volume

    def test_volume_is_imported(self):
        volume = self.tenant_backend.import_volume(
            self.backend_volume_id, project=self.fixture.project, save=True
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
            service_settings=self.fixture.openstack_tenant_service_settings,
            project=self.fixture.project,
        )
        self.backend_volume.attachments = [dict(server_id=vm.backend_id)]
        volume = self.tenant_backend.import_volume(
            self.backend_volume_id, project=self.fixture.project, save=True
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


class PullVolumeTest(BaseBackendTest):
    def setUp(self):
        super().setUp()
        self.backend_volume_id = "backend_id"
        self.backend_volume = self._get_valid_volume(self.backend_volume_id)

        self.mocked_cinder.volumes.get.return_value = self.backend_volume

    def test_volume_instance_is_pulled(self):
        vm = factories.InstanceFactory(
            backend_id="instance_backend_id",
            service_settings=self.fixture.openstack_tenant_service_settings,
            project=self.fixture.project,
        )
        volume = factories.VolumeFactory(
            backend_id=self.backend_volume_id,
            instance=vm,
            service_settings=self.fixture.openstack_tenant_service_settings,
            project=self.fixture.project,
        )
        self.backend_volume.attachments = [dict(server_id=vm.backend_id)]
        self.tenant_backend.pull_volume(volume)
        volume.refresh_from_db()

        self.assertEqual(volume.instance, vm)

    def test_volume_image_is_pulled(self):
        volume = factories.VolumeFactory(
            backend_id=self.backend_volume_id,
            service_settings=self.fixture.openstack_tenant_service_settings,
            project=self.fixture.project,
        )
        image = factories.ImageFactory(settings=self.settings)
        self.backend_volume.volume_image_metadata = {"image_id": image.backend_id}
        self.tenant_backend.pull_volume(volume)
        volume.refresh_from_db()

        self.assertEqual(volume.image, image)

    def test_volume_image_is_not_pulled(self):
        volume = factories.VolumeFactory(
            backend_id=self.backend_volume_id,
            service_settings=self.fixture.openstack_tenant_service_settings,
            project=self.fixture.project,
        )
        self.backend_volume.volume_image_metadata = {}
        self.tenant_backend.pull_volume(volume)
        volume.refresh_from_db()

        self.assertEqual(volume.image, None)


class PullInstanceAvailabilityZonesTest(BaseBackendTest):
    def test_default_zone_is_not_pulled(self):
        self.mocked_nova.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": "nova", "zoneState": {"available": True}})
        ]
        self.tenant_backend.pull_instance_availability_zones()
        self.assertEqual(models.InstanceAvailabilityZone.objects.count(), 0)

    def test_missing_zone_is_created(self):
        self.mocked_nova.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": "AZ_T1", "zoneState": {"available": True}})
        ]

        self.tenant_backend.pull_instance_availability_zones()
        self.assertEqual(models.InstanceAvailabilityZone.objects.count(), 1)

        zone = models.InstanceAvailabilityZone.objects.get()
        self.assertEqual(zone.name, "AZ_T1")
        self.assertTrue(zone.available)

    def test_stale_zone_is_removed(self):
        self.fixture.instance_availability_zone
        self.mocked_nova.availability_zones.list.return_value = []

        self.tenant_backend.pull_instance_availability_zones()
        self.assertEqual(models.InstanceAvailabilityZone.objects.count(), 0)

    def test_existing_zone_is_updated(self):
        zone = self.fixture.instance_availability_zone
        self.mocked_nova.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": zone.name, "zoneState": {"available": False}})
        ]

        self.tenant_backend.pull_instance_availability_zones()
        self.assertEqual(models.InstanceAvailabilityZone.objects.count(), 1)

        zone = models.InstanceAvailabilityZone.objects.get()
        self.assertFalse(zone.available)


class PullVolumeAvailabilityZonesTest(BaseBackendTest):
    def setUp(self):
        super().setUp()
        self.tenant_backend.is_volume_availability_zone_supported = lambda: True

    def test_default_zone_is_not_pulled(self):
        self.mocked_cinder.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": "nova", "zoneState": {"available": True}})
        ]
        self.tenant_backend.pull_volume_availability_zones()
        self.assertEqual(models.VolumeAvailabilityZone.objects.count(), 0)

    def test_missing_zone_is_created(self):
        self.mocked_cinder.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": "AZ_T1", "zoneState": {"available": True}})
        ]

        self.tenant_backend.pull_volume_availability_zones()
        self.assertEqual(models.VolumeAvailabilityZone.objects.count(), 1)

        zone = models.VolumeAvailabilityZone.objects.get()
        self.assertEqual(zone.name, "AZ_T1")
        self.assertTrue(zone.available)

    def test_stale_zone_is_removed(self):
        self.fixture.volume_availability_zone
        self.mocked_cinder.availability_zones.list.return_value = []

        self.tenant_backend.pull_volume_availability_zones()
        self.assertEqual(models.VolumeAvailabilityZone.objects.count(), 0)

    def test_existing_zone_is_updated(self):
        zone = self.fixture.volume_availability_zone
        self.mocked_cinder.availability_zones.list.return_value = [
            mock.Mock(**{"zoneName": zone.name, "zoneState": {"available": False}})
        ]

        self.tenant_backend.pull_volume_availability_zones()
        self.assertEqual(models.VolumeAvailabilityZone.objects.count(), 1)

        zone = models.VolumeAvailabilityZone.objects.get()
        self.assertFalse(zone.available)


class PullInstanceTest(BaseBackendTest):
    def setUp(self):
        super().setUp()

        class MockFlavor:
            name = "flavor_name"
            disk = 102400
            ram = 10240
            vcpus = 1

        class MockInstance:
            name = "instance_name"
            id = "instance_id"
            created = "2017-08-10"
            key_name = "key_name"
            flavor = {"id": "flavor_id"}
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
        self.mocked_nova.flavors.get.return_value = MockFlavor

    def test_availability_zone_is_pulled(self):
        zone = self.fixture.instance_availability_zone
        zone.name = "AZ_TST"
        zone.save()

        instance = self.fixture.instance

        self.tenant_backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.availability_zone, zone)

    def test_invalid_availability_zone_is_skipped(self):
        instance = self.fixture.instance

        self.tenant_backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.availability_zone, None)

    def test_error_message_is_synchronized(self):
        instance = self.fixture.instance

        self.tenant_backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.error_message, "OpenStack Nova error.")

    def test_existing_error_message_is_preserved_if_defined(self):
        del self.mocked_nova.servers.get.return_value.fault
        instance = self.fixture.instance
        instance.error_message = "Waldur error."
        instance.save()

        self.tenant_backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.error_message, "Waldur error.")

    def test_hypervisor_hostname_is_synchronized(self):
        instance = self.fixture.instance

        self.tenant_backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.hypervisor_hostname, "aio1.openstack.local")


class PullInstancePortsTest(BaseBackendTest):
    def setup_neutron(self, port_id, device_id, subnet_id):
        self.mocked_neutron.list_ports.return_value = {
            "ports": [
                {
                    "id": port_id,
                    "mac_address": "DC-D6-5E-9B-49-70",
                    "device_id": device_id,
                    "device_owner": "compute:nova",
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
        self.tenant_backend.pull_instance_ports(instance)

        # Assert
        port.refresh_from_db()
        self.assertEqual(port.backend_id, "port_id")

    def test_missing_ports_are_created(self):
        # Arrange
        instance = self.fixture.instance
        subnet = self.fixture.subnet
        self.setup_neutron("port_id", instance.backend_id, subnet.backend_id)

        # Act
        self.tenant_backend.pull_instance_ports(instance)

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
        self.tenant_backend.pull_instance_ports(instance)

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
        self.tenant_backend.pull_instance_ports(vm)

        # Assert
        self.assertEqual(vm.ports.count(), 1)
        self.assertEqual(vm.ports.first(), ip2)

    def test_existing_ports_are_updated(self):
        # Arrange
        instance = self.fixture.instance
        port = self.fixture.port
        self.setup_neutron(port.backend_id, instance.backend_id, port.subnet.backend_id)

        # Act
        self.tenant_backend.pull_instance_ports(instance)

        # Assert
        port.refresh_from_db()
        self.assertEqual(port.mac_address, "DC-D6-5E-9B-49-70")
        self.assertEqual(port.fixed_ips[0]["ip_address"], "10.0.0.2")

    def test_shared_ports_are_reassigned(self):
        # Arrange
        vm1 = factories.InstanceFactory(
            service_settings=self.fixture.openstack_tenant_service_settings,
            project=self.fixture.project,
        )
        vm2 = factories.InstanceFactory(
            service_settings=self.fixture.openstack_tenant_service_settings,
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
        self.tenant_backend.pull_instance_ports(vm1)

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
        self.os_backend.pull_ports()

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
        self.os_backend.pull_ports()

        # Assert
        self.assertEqual(instance.ports.count(), 0)

    def test_existing_ports_are_updated(self):
        # Arrange
        instance = self.fixture.instance
        port = self.fixture.port
        self.setup_neutron(port.backend_id, instance.backend_id, port.subnet.backend_id)

        # Act
        self.os_backend.pull_ports()

        # Assert
        port.refresh_from_db()
        self.assertEqual(port.mac_address, "DC-D6-5E-9B-49-70")
        self.assertEqual(port.fixed_ips[0]["ip_address"], "10.0.0.2")

    def test_even_if_port_is_not_connected_it_is_not_skipped(self):
        # Arrange
        self.setup_neutron("port_id", "", self.fixture.port.subnet.backend_id)

        # Act
        self.os_backend.pull_ports()

        # Assert
        ports = Port.objects.filter(subnet=self.fixture.subnet)
        self.assertEqual(ports.count(), 1)

        port = ports.first()
        self.assertEqual(port.instance, None)
        self.assertEqual(port.backend_id, "port_id")
        self.assertEqual(port.mac_address, "DC-D6-5E-9B-49-70")
        self.assertEqual(port.fixed_ips[0]["ip_address"], "10.0.0.2")

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
                    "network_id": "network_id",
                    "security_groups": [],
                },
            ]
        }

        # Act
        self.os_backend.pull_ports()

        # Assert
        self.assertEqual(2, instance.ports.count())

        actual_subnets = set(instance.ports.values_list("subnet_id", flat=True))
        self.assertEqual({subnet.id}, actual_subnets)

        actual_addresses = list(instance.ports.values_list("fixed_ips", flat=True))
        self.assertEqual(
            [
                [{"ip_address": "10.0.0.2", "subnet_id": subnet_id}],
                [{"ip_address": "10.0.0.3", "subnet_id": subnet_id}],
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
                    "network_id": "network_id",
                    "security_groups": [],
                }
            ]
        }

        # Act
        self.os_backend.pull_ports()

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

        def get_volume(backend_id):
            return self._get_valid_flavor(backend_id=backend_id)

        self.mocked_nova.servers.list.return_value = instances
        self.mocked_nova.flavors.get.side_effect = get_volume

        result = self.tenant_backend.get_instances()

        returned_backend_ids = [item.backend_id for item in result]
        expected_backend_ids = [item.id for item in instances]
        self.assertEqual(sorted(returned_backend_ids), sorted(expected_backend_ids))


class ImportInstanceTest(BaseBackendTest):
    def setUp(self):
        super().setUp()
        self.backend_id = "instance_id"
        self.backend_instance = self._get_valid_instance(self.backend_id)
        self.mocked_nova.servers.get.return_value = self.backend_instance

        backend_flavor = self._get_valid_flavor(self.backend_id)
        self.backend_instance.flavor = backend_flavor._info
        self.mocked_nova.flavors.get.return_value = backend_flavor

    def test_backend_instance_without_volumes_is_imported(self):
        self.mocked_nova.volumes.get_server_volumes.return_value = []

        instance = self.tenant_backend.import_instance(
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
            service_settings=self.fixture.openstack_tenant_service_settings,
            project=self.fixture.project,
        )
        backend_volume = self._get_valid_volume(backend_id=expected_volume.backend_id)
        backend_volume.volumeId = backend_volume.id
        self.mocked_nova.volumes.get_server_volumes.return_value = [backend_volume]
        self.mocked_cinder.volumes.get.return_value = backend_volume

        instance = self.tenant_backend.import_instance(
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

        instance = self.tenant_backend.import_instance(
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

        instance = self.tenant_backend.import_instance(
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
            subnet=subnet,
            backend_id="port_id1",
            fixed_ips=[{"ip_address": "192.168.42.42", "subnet_id": subnet.backend_id}],
        )

        ip2 = PortFactory(
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
        self.tenant_backend.pull_instance_floating_ips(instance)

        # Assert
        self.assertEqual(1, instance.floating_ips.count())

        fip.refresh_from_db()
        self.assertEqual(ip2, fip.port)


class CreateInstanceTest(VolumesBaseTest):
    def setUp(self):
        super().setUp()
        self.flavor_id = "small_flavor"
        backend_flavor = self._get_valid_flavor(self.flavor_id)
        self.mocked_nova.flavors.get.return_value = backend_flavor
        self.mocked_nova.servers.create.return_value.id = uuid.uuid4()

    def test_zone_name_is_passed_to_nova_client(self):
        # Arrange
        zone = self.fixture.instance_availability_zone
        vm = self.fixture.instance
        vm.availability_zone = zone
        vm.save()

        # Act
        self.tenant_backend.create_instance(vm, self.flavor_id)

        # Assert
        kwargs = self.mocked_nova.servers.create.mock_calls[0][2]
        self.assertEqual(kwargs["availability_zone"], zone.name)

    def test_default_zone_name_is_passed_to_nova_client(self):
        # Arrange
        self.settings.options["availability_zone"] = "default_availability_zone"

        # Act
        self.tenant_backend.create_instance(self.fixture.instance, self.flavor_id)

        # Assert
        kwargs = self.mocked_nova.servers.create.mock_calls[0][2]
        self.assertEqual(kwargs["availability_zone"], "default_availability_zone")
