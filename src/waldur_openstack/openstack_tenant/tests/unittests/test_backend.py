from __future__ import unicode_literals

from django.test import TestCase
from cinderclient.v2.volumes import Volume
from novaclient.v2.servers import Server
from novaclient.v2.flavors import Flavor
import mock

from waldur_openstack.openstack_tenant.backend import OpenStackTenantBackend
from waldur_openstack.openstack_tenant import models

from .. import fixtures, factories


class BaseBackendTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.settings = self.fixture.openstack_tenant_service_settings
        self.tenant = self.fixture.openstack_tenant_service_settings.scope
        self.tenant_backend = OpenStackTenantBackend(self.settings)
        self.neutron_client_mock = mock.Mock()
        self.cinder_client_mock = mock.Mock()
        self.nova_client_mock = mock.Mock()
        self.tenant_backend.neutron_client = self.neutron_client_mock
        self.tenant_backend.cinder_client = self.cinder_client_mock
        self.tenant_backend.nova_client = self.nova_client_mock

    def _get_valid_volume(self, backend_id):
        return Volume(manager=None, info=dict(
            name='volume-%s' % backend_id,
            size=1,
            metadata='',
            description='',
            volume_type='',
            status='OK',
            id=backend_id,
            bootable='true',
        ))

    def _get_valid_instance(self, backend_id):
        return Server(manager=None, info={
            'id': backend_id,
            'name': 'server-%s' % backend_id,
            'status': 'ACTIVE',
            'key_name': '',
            'created': '2012-04-23T08:10:00Z',
            'OS-SRV-USG:launched_at': '2012-04-23T09:15'
        })

    def _get_valid_flavor(self, backend_id):
        return Flavor(manager=None, info=dict(
            name='m1.small',
            disk=10,
            vcpus=2,
            ram=4096,
            id=backend_id,
        ))


class PullFloatingIPTest(BaseBackendTest):

    def _get_valid_new_backend_ip(self, internal_ip):
        return dict(floatingips=[{
            'floating_ip_address': '0.0.0.0',
            'floating_network_id': 'new_backend_network_id',
            'status': 'DOWN',
            'id': 'new_backend_id',
            'port_id': internal_ip.backend_id
        }])

    def test_floating_ip_is_not_created_if_internal_ip_is_missing(self):
        internal_ip = factories.InternalIPFactory(instance=self.fixture.instance)
        backend_floating_ips = self._get_valid_new_backend_ip(internal_ip)
        internal_ip.delete()
        self.neutron_client_mock.list_floatingips.return_value = backend_floating_ips
        self.assertEqual(models.FloatingIP.objects.count(), 0)

        self.tenant_backend.pull_floating_ips()

        self.assertEqual(models.FloatingIP.objects.count(), 0)

    def test_floating_ip_is_not_updated_if_internal_ip_is_missing(self):
        internal_ip = factories.InternalIPFactory(instance=self.fixture.instance)
        backend_floating_ips = self._get_valid_new_backend_ip(internal_ip)
        internal_ip.delete()
        self.neutron_client_mock.list_floatingips.return_value = backend_floating_ips
        backend_ip = backend_floating_ips['floatingips'][0]
        floating_ip = factories.FloatingIPFactory(settings=self.settings,
                                                  backend_id=backend_ip['id'],
                                                  name='old_name',
                                                  runtime_state='old_status',
                                                  backend_network_id='old_backend_network_id',
                                                  address='127.0.0.1')
        self.assertEqual(models.FloatingIP.objects.count(), 1)

        self.tenant_backend.pull_floating_ips()

        self.assertEqual(models.FloatingIP.objects.count(), 1)
        floating_ip.refresh_from_db()
        self.assertNotEqual(floating_ip.address, backend_ip['floating_ip_address'])
        self.assertNotEqual(floating_ip.name, backend_ip['floating_ip_address'])
        self.assertNotEqual(floating_ip.runtime_state, backend_ip['status'])
        self.assertNotEqual(floating_ip.backend_network_id, backend_ip['floating_network_id'])

    def test_floating_ip_is_updated_if_internal_ip_exists_even_if_not_connected_to_instance(self):
        internal_ip = factories.InternalIPFactory(subnet=self.fixture.subnet)
        backend_floating_ips = self._get_valid_new_backend_ip(internal_ip)
        self.neutron_client_mock.list_floatingips.return_value = backend_floating_ips

        backend_ip = backend_floating_ips['floatingips'][0]
        floating_ip = factories.FloatingIPFactory(settings=self.settings,
                                                  backend_id=backend_ip['id'],
                                                  name='old_name',
                                                  runtime_state='old_status',
                                                  backend_network_id='old_backend_network_id',
                                                  address='127.0.0.1')

        self.tenant_backend.pull_floating_ips()

        floating_ip.refresh_from_db()

        self.assertEqual(models.FloatingIP.objects.count(), 1)
        self.assertEqual(floating_ip.address, backend_ip['floating_ip_address'])
        self.assertEqual(floating_ip.runtime_state, backend_ip['status'])
        self.assertEqual(floating_ip.backend_network_id, backend_ip['floating_network_id'])
        self.assertEqual(floating_ip.internal_ip, internal_ip)

    def test_floating_ip_is_created_if_it_does_not_exist(self):
        internal_ip = factories.InternalIPFactory(subnet=self.fixture.subnet, instance=self.fixture.instance)
        backend_floating_ips = self._get_valid_new_backend_ip(internal_ip)
        backend_ip = backend_floating_ips['floatingips'][0]
        self.neutron_client_mock.list_floatingips.return_value = backend_floating_ips

        self.tenant_backend.pull_floating_ips()

        self.assertEqual(models.FloatingIP.objects.count(), 1)
        created_ip = models.FloatingIP.objects.get(backend_id=backend_ip['id'])
        self.assertEqual(created_ip.runtime_state, backend_ip['status'])
        self.assertEqual(created_ip.backend_network_id, backend_ip['floating_network_id'])
        self.assertEqual(created_ip.address, backend_ip['floating_ip_address'])

    def test_floating_ip_is_deleted_if_it_is_not_returned_by_neutron(self):
        floating_ip = factories.FloatingIPFactory(settings=self.settings)
        self.neutron_client_mock.list_floatingips.return_value = dict(floatingips=[])

        self.tenant_backend.pull_floating_ips()

        self.assertFalse(models.FloatingIP.objects.filter(id=floating_ip.id).exists())

    def test_floating_ip_is_not_updated_if_it_is_in_booked_state(self):
        internal_ip = factories.InternalIPFactory(instance=self.fixture.instance)
        backend_floating_ips = self._get_valid_new_backend_ip(internal_ip)
        self.neutron_client_mock.list_floatingips.return_value = backend_floating_ips
        backend_ip = backend_floating_ips['floatingips'][0]
        expected_name = 'booked ip'
        expected_address = '127.0.0.1'
        expected_runtime_state = 'booked_state'
        booked_ip = factories.FloatingIPFactory(is_booked=True,
                                                settings=self.settings,
                                                backend_id=backend_ip['id'],
                                                name=expected_name,
                                                address=expected_address,
                                                runtime_state=expected_runtime_state)

        self.tenant_backend.pull_floating_ips()

        booked_ip.refresh_from_db()
        self.assertEqual(booked_ip.name, expected_name)
        self.assertEqual(booked_ip.address, expected_address)
        self.assertEqual(booked_ip.runtime_state, expected_runtime_state)

    def test_floating_ip_is_not_duplicated_if_it_is_in_booked_state(self):
        internal_ip = factories.InternalIPFactory(instance=self.fixture.instance)
        backend_floating_ips = self._get_valid_new_backend_ip(internal_ip)
        self.neutron_client_mock.list_floatingips.return_value = backend_floating_ips
        backend_ip = backend_floating_ips['floatingips'][0]
        factories.FloatingIPFactory(
            is_booked=True,
            settings=self.settings,
            backend_id=backend_ip['id'],
            name='booked ip',
            address=backend_ip['floating_ip_address'],
            runtime_state='booked_state')

        self.tenant_backend.pull_floating_ips()

        backend_ip_address = backend_ip['floating_ip_address']
        self.assertEqual(models.FloatingIP.objects.filter(address=backend_ip_address).count(), 1)

    def test_floating_ip_name_is_not_update_if_it_was_set_by_user(self):
        internal_ip = factories.InternalIPFactory(instance=self.fixture.instance)
        backend_floating_ips = self._get_valid_new_backend_ip(internal_ip)
        self.neutron_client_mock.list_floatingips.return_value = backend_floating_ips
        backend_ip = backend_floating_ips['floatingips'][0]
        expected_name = 'user defined ip'
        floating_ip = factories.FloatingIPFactory(
            settings=self.settings,
            backend_id=backend_ip['id'],
            name=expected_name)

        self.tenant_backend.pull_floating_ips()

        floating_ip.refresh_from_db()
        self.assertNotEqual(floating_ip.address, floating_ip.name)
        self.assertEqual(floating_ip.name, expected_name)


class PullSecurityGroupsTest(BaseBackendTest):

    def setUp(self):
        super(PullSecurityGroupsTest, self).setUp()
        self.backend_security_groups = {
            'security_groups': [
                {
                    'id': 'backend_id',
                    'name': 'Default',
                    'description': 'Default security group',
                    'security_group_rules': [],
                }
            ]
        }
        self.neutron_client_mock.list_security_groups.return_value = self.backend_security_groups

    def test_pull_creates_missing_security_group(self):
        self.tenant_backend.pull_security_groups()

        self.neutron_client_mock.list_security_groups.assert_called_once_with(
            tenant_id=self.tenant.backend_id
        )
        self.assertEqual(models.SecurityGroup.objects.count(), 1)
        security_group = models.SecurityGroup.objects.get(
            settings=self.settings,
            backend_id='backend_id',
        )
        self.assertEqual(security_group.name, 'Default')
        self.assertEqual(security_group.description, 'Default security group')

    def test_pull_creates_missing_security_group_rule(self):
        self.backend_security_groups['security_groups'][0]['security_group_rules'] = [
            {
                'id': 'security_group_id',
                'direction': 'ingress',
                'port_range_min': 80,
                'port_range_max': 80,
                'protocol': 'tcp',
                'remote_ip_prefix': '0.0.0.0/0',
            }
        ]
        self.tenant_backend.pull_security_groups()

        self.assertEqual(models.SecurityGroupRule.objects.count(), 1)
        security_group = models.SecurityGroup.objects.get(
            settings=self.settings,
            backend_id='backend_id',
        )
        security_group_rule = models.SecurityGroupRule.objects.get(
            security_group=security_group,
            backend_id='security_group_id',
        )
        self.assertEqual(security_group_rule.from_port, 80)
        self.assertEqual(security_group_rule.to_port, 80)
        self.assertEqual(security_group_rule.protocol, 'tcp')
        self.assertEqual(security_group_rule.cidr, '0.0.0.0/0')

    def test_stale_security_groups_are_deleted(self):
        factories.SecurityGroupFactory(settings=self.settings)
        self.neutron_client_mock.list_security_groups.return_value = dict(security_groups=[])
        self.tenant_backend.pull_security_groups()
        self.assertEqual(models.SecurityGroup.objects.count(), 0)

    def test_security_groups_are_updated(self):
        security_group = factories.SecurityGroupFactory(
            settings=self.settings,
            backend_id='backend_id',
            name='Old name',
        )
        self.tenant_backend.pull_security_groups()
        security_group.refresh_from_db()
        self.assertEqual(security_group.name, 'Default')


class PullNetworksTest(BaseBackendTest):

    def setUp(self):
        super(PullNetworksTest, self).setUp()
        self.backend_networks = {
            'networks': [
                {
                    'id': 'backend_id',
                    'name': 'Private',
                    'description': 'Internal network',
                }
            ]
        }
        self.neutron_client_mock.list_networks.return_value = self.backend_networks

    def test_missing_networks_are_created(self):
        self.tenant_backend.pull_networks()

        self.assertEqual(models.Network.objects.count(), 1)
        network = models.Network.objects.get(
            settings=self.settings,
            backend_id='backend_id',
        )
        self.assertEqual(network.name, 'Private')
        self.assertEqual(network.description, 'Internal network')

    def test_stale_networks_are_deleted(self):
        factories.NetworkFactory(settings=self.settings)
        self.neutron_client_mock.list_networks.return_value = dict(networks=[])
        self.tenant_backend.pull_networks()
        self.assertEqual(models.Network.objects.count(), 0)

    def test_existing_networks_are_updated(self):
        network = factories.NetworkFactory(
            settings=self.settings,
            backend_id='backend_id',
            name='Old name',
        )
        self.tenant_backend.pull_networks()
        network.refresh_from_db()
        self.assertEqual(network.name, 'Private')


class PullSubnetsTest(BaseBackendTest):

    def setUp(self):
        super(PullSubnetsTest, self).setUp()
        self.network = factories.NetworkFactory(
            settings=self.settings,
            backend_id='network_id'
        )
        self.backend_subnets = {
            'subnets': [
                {
                    'id': 'backend_id',
                    'network_id': 'network_id',
                    'name': 'subnet-1',
                    'description': '',
                    'cidr': '192.168.42.0/24',
                    'ip_version': 4,
                    'allocation_pools': [
                        {
                            'start': '192.168.42.10',
                            'end': '192.168.42.100',
                        }
                    ],
                }
            ]
        }
        self.neutron_client_mock.list_subnets.return_value = self.backend_subnets

    def test_missing_subnets_are_created(self):
        self.tenant_backend.pull_subnets()

        self.neutron_client_mock.list_subnets.assert_called_once_with(
            tenant_id=self.tenant.backend_id
        )
        self.assertEqual(models.SubNet.objects.count(), 1)
        subnet = models.SubNet.objects.get(
            settings=self.settings,
            backend_id='backend_id',
            network=self.network,
        )
        self.assertEqual(subnet.name, 'subnet-1')
        self.assertEqual(subnet.cidr, '192.168.42.0/24')
        self.assertEqual(subnet.allocation_pools, [
            {
                'start': '192.168.42.10',
                'end': '192.168.42.100',
            }
        ])

    def test_subnet_is_not_pulled_if_network_is_not_pulled_yet(self):
        self.network.delete()
        self.tenant_backend.pull_subnets()
        self.assertEqual(models.SubNet.objects.count(), 0)

    def test_stale_subnets_are_deleted(self):
        factories.NetworkFactory(settings=self.settings)
        self.neutron_client_mock.list_subnets.return_value = dict(subnets=[])
        self.tenant_backend.pull_subnets()
        self.assertEqual(models.SubNet.objects.count(), 0)

    def test_existing_subnets_are_updated(self):
        subnet = factories.SubNetFactory(
            settings=self.settings,
            backend_id='backend_id',
            name='Old name',
            network=self.network,
        )
        self.tenant_backend.pull_subnets()
        subnet.refresh_from_db()
        self.assertEqual(subnet.name, 'subnet-1')


class GetVolumesTest(BaseBackendTest):

    def _generate_volumes(self, backend=False, count=1):
        volumes = []
        for i in range(count):
            volume = factories.VolumeFactory()
            backend_volume = self._get_valid_volume(backend_id=volume.backend_id)
            if backend:
                volume.delete()
            volumes.append(backend_volume)

        return volumes

    def test_all_backend_volumes_are_returned(self):
        backend_volumes = self._generate_volumes(backend=True, count=2)
        volumes = backend_volumes + self._generate_volumes()
        self.cinder_client_mock.volumes.list.return_value = volumes

        result = self.tenant_backend.get_volumes()

        returned_backend_ids = [item.backend_id for item in result]
        expected_backend_ids = [item.id for item in volumes]
        self.assertItemsEqual(returned_backend_ids, expected_backend_ids)


class ImportVolumeTest(BaseBackendTest):

    def setUp(self):
        super(ImportVolumeTest, self).setUp()
        self.spl = self.fixture.spl
        self.backend_volume_id = 'backend_id'
        self.backend_volume = self._get_valid_volume(self.backend_volume_id)

        self.cinder_client_mock.volumes.get.return_value = self.backend_volume

    def test_volume_is_imported(self):
        volume = self.tenant_backend.import_volume(self.backend_volume_id, save=True, service_project_link=self.spl)

        self.assertTrue(models.Volume.objects.filter(backend_id=self.backend_volume_id).exists())
        self.assertEqual(models.Volume.objects.get(backend_id=self.backend_volume_id).uuid, volume.uuid)
        self.assertEqual(volume.name, self.backend_volume.name)

    def test_volume_instance_is_not_created_during_import(self):
        vm = factories.InstanceFactory(backend_id='instance_backend_id', service_project_link=self.spl)
        self.backend_volume.attachments = [
            dict(server_id=vm.backend_id)
        ]
        volume = self.tenant_backend.import_volume(self.backend_volume_id, save=True, service_project_link=self.spl)

        self.assertIsNotNone(volume.instance)
        self.assertTrue(models.Volume.objects.filter(backend_id=self.backend_volume_id).exists())
        self.assertEqual(models.Volume.objects.get(backend_id=self.backend_volume_id).uuid, volume.uuid)
        self.assertEqual(volume.name, self.backend_volume.name)


class PullInstanceTest(BaseBackendTest):

    def setUp(self):
        super(PullInstanceTest, self).setUp()

        class MockFlavor(object):
            name = 'flavor_name'
            disk = 102400
            ram = 10240
            vcpus = 1

        class MockInstance(object):
            name = 'instance_name'
            id = 'instance_id'
            created = '2017-08-10'
            key_name = 'key_name'
            flavor = {'id': 'flavor_id'}
            status = 'ERRED'
            fault = {'message': 'OpenStack Nova error.'}

            def to_dict(self):
                return {}

        self.nova_client_mock = mock.Mock()
        self.tenant_backend.nova_client = self.nova_client_mock

        self.nova_client_mock.servers.get.return_value = MockInstance
        self.nova_client_mock.volumes.get_server_volumes.return_value = []
        self.nova_client_mock.flavors.get.return_value = MockFlavor

    def test_error_message_is_synchronized(self):
        instance = self.fixture.instance

        self.tenant_backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.error_message, 'OpenStack Nova error.')

    def test_existing_error_message_is_preserved_if_defined(self):
        del self.nova_client_mock.servers.get.return_value.fault
        instance = self.fixture.instance
        instance.error_message = 'Waldur error.'
        instance.save()

        self.tenant_backend.pull_instance(instance)
        instance.refresh_from_db()

        self.assertEqual(instance.error_message, 'Waldur error.')


class PullInstanceInternalIpsTest(BaseBackendTest):
    def setup_neutron(self, port_id, device_id, subnet_id):
        self.neutron_client_mock.list_ports.return_value = {
            'ports': [
                {
                    'id': port_id,
                    'mac_address': 'DC-D6-5E-9B-49-70',
                    'device_id': device_id,
                    'device_owner': 'compute:nova',
                    'fixed_ips': [
                        {
                            'ip_address': '10.0.0.2',
                            'subnet_id': subnet_id,
                        }
                    ]
                }
            ]
        }

    def test_pending_internal_ips_are_updated_with_backend_id(self):
        # Arrange
        instance = self.fixture.instance
        internal_ip = self.fixture.internal_ip
        internal_ip.backend_id = ''
        internal_ip.save()
        self.setup_neutron('port_id', instance.backend_id, internal_ip.subnet.backend_id)

        # Act
        self.tenant_backend.pull_instance_internal_ips(instance)

        # Assert
        internal_ip.refresh_from_db()
        self.assertEqual(internal_ip.backend_id, 'port_id')

    def test_missing_internal_ips_are_created(self):
        # Arrange
        instance = self.fixture.instance
        subnet = self.fixture.subnet
        self.setup_neutron('port_id', instance.backend_id, subnet.backend_id)

        # Act
        self.tenant_backend.pull_instance_internal_ips(instance)

        # Assert
        self.assertEqual(instance.internal_ips_set.count(), 1)
        internal_ip = instance.internal_ips_set.first()
        self.assertEqual(internal_ip.backend_id, 'port_id')
        self.assertEqual(internal_ip.subnet, subnet)

    def test_stale_internal_ips_are_deleted(self):
        # Arrange
        instance = self.fixture.instance

        self.neutron_client_mock.list_ports.return_value = {
            'ports': []
        }

        # Act
        self.tenant_backend.pull_instance_internal_ips(instance)

        # Assert
        self.assertEqual(instance.internal_ips_set.count(), 0)

    def test_existing_internal_ips_are_updated(self):
        # Arrange
        instance = self.fixture.instance
        internal_ip = self.fixture.internal_ip
        self.setup_neutron(internal_ip.backend_id, instance.backend_id, internal_ip.subnet.backend_id)

        # Act
        self.tenant_backend.pull_instance_internal_ips(instance)

        # Assert
        internal_ip.refresh_from_db()
        self.assertEqual(internal_ip.mac_address, 'DC-D6-5E-9B-49-70')
        self.assertEqual(internal_ip.ip4_address, '10.0.0.2')


class PullInternalIpsTest(BaseBackendTest):
    def setup_neutron(self, port_id, device_id, subnet_id):
        self.neutron_client_mock.list_ports.return_value = {
            'ports': [
                {
                    'id': port_id,
                    'mac_address': 'DC-D6-5E-9B-49-70',
                    'device_id': device_id,
                    'device_owner': 'compute:nova',
                    'fixed_ips': [
                        {
                            'ip_address': '10.0.0.2',
                            'subnet_id': subnet_id,
                        }
                    ]
                }
            ]
        }

    def test_pending_internal_ips_are_updated_with_backend_id(self):
        # Arrange
        instance = self.fixture.instance
        internal_ip = self.fixture.internal_ip
        internal_ip.backend_id = ''
        internal_ip.save()
        self.setup_neutron('port_id', instance.backend_id, internal_ip.subnet.backend_id)

        # Act
        self.tenant_backend.pull_internal_ips()

        # Assert
        internal_ip.refresh_from_db()
        self.assertEqual(internal_ip.backend_id, 'port_id')

    def test_missing_internal_ips_are_created(self):
        # Arrange
        instance = self.fixture.instance
        subnet = self.fixture.subnet
        self.setup_neutron('port_id', instance.backend_id, subnet.backend_id)

        # Act
        self.tenant_backend.pull_internal_ips()

        # Assert
        self.assertEqual(instance.internal_ips_set.count(), 1)
        internal_ip = instance.internal_ips_set.first()
        self.assertEqual(internal_ip.backend_id, 'port_id')
        self.assertEqual(internal_ip.subnet, subnet)

    def test_stale_internal_ips_are_deleted(self):
        # Arrange
        instance = self.fixture.instance

        self.neutron_client_mock.list_ports.return_value = {
            'ports': []
        }

        # Act
        self.tenant_backend.pull_internal_ips()

        # Assert
        self.assertEqual(instance.internal_ips_set.count(), 0)

    def test_existing_internal_ips_are_updated(self):
        # Arrange
        instance = self.fixture.instance
        internal_ip = self.fixture.internal_ip
        self.setup_neutron(internal_ip.backend_id, instance.backend_id, internal_ip.subnet.backend_id)

        # Act
        self.tenant_backend.pull_internal_ips()

        # Assert
        internal_ip.refresh_from_db()
        self.assertEqual(internal_ip.mac_address, 'DC-D6-5E-9B-49-70')
        self.assertEqual(internal_ip.ip4_address, '10.0.0.2')

    def test_even_if_internal_ip_is_not_connected_it_is_not_skipped(self):
        # Arrange
        self.setup_neutron('port_id', '', self.fixture.internal_ip.subnet.backend_id)

        # Act
        self.tenant_backend.pull_internal_ips()

        # Assert
        internal_ips = models.InternalIP.objects.filter(subnet=self.fixture.subnet)
        self.assertEqual(internal_ips.count(), 1)

        internal_ip = internal_ips.first()
        self.assertEqual(internal_ip.instance, None)
        self.assertEqual(internal_ip.backend_id, 'port_id')
        self.assertEqual(internal_ip.mac_address, 'DC-D6-5E-9B-49-70')
        self.assertEqual(internal_ip.ip4_address, '10.0.0.2')


class GetInstancesTest(BaseBackendTest):

    def setUp(self):
        super(GetInstancesTest, self).setUp()

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
        flavors = []
        for instance in instances:
            flavor = self._get_valid_flavor(backend_id=instance.id)
            instance.flavor = flavor._info
            flavors.append(flavor)

        self.nova_client_mock.servers.list.return_value = instances
        self.nova_client_mock.flavors.list.return_value = flavors

        result = self.tenant_backend.get_instances()

        returned_backend_ids = [item.backend_id for item in result]
        expected_backend_ids = [item.id for item in instances]
        self.assertItemsEqual(returned_backend_ids, expected_backend_ids)


class ImportInstanceTest(BaseBackendTest):

    def setUp(self):
        super(ImportInstanceTest, self).setUp()
        self.spl = self.fixture.spl
        self.backend_id = 'instance_id'
        self.backend_instance = self._get_valid_instance(self.backend_id)
        self.nova_client_mock.servers.get.return_value = self.backend_instance

        backend_flavor = self._get_valid_flavor(self.backend_id)
        self.backend_instance.flavor = backend_flavor._info
        self.nova_client_mock.flavors.get.return_value = backend_flavor

    def test_backend_instance_without_volumes_is_imported(self):
        self.nova_client_mock.volumes.get_server_volumes.return_value = []

        instance = self.tenant_backend.import_instance(self.backend_id, save=True, service_project_link=self.spl)

        self.assertEquals(instance.backend_id, self.backend_id)
        self.assertTrue(models.Instance.objects.filter(backend_id=self.backend_id).exists())
        self.assertEquals(models.Instance.objects.get(backend_id=self.backend_id).uuid, instance.uuid)
        self.assertEquals(instance.name, self.backend_instance.name)

    def test_volume_is_attached_to_imported_instance_if_they_are_registered(self):
        expected_volume = factories.VolumeFactory(service_project_link=self.spl)
        backend_volume = self._get_valid_volume(backend_id=expected_volume.backend_id)
        backend_volume.volumeId = backend_volume.id
        self.nova_client_mock.volumes.get_server_volumes.return_value = [backend_volume]
        self.cinder_client_mock.volumes.get.return_value = backend_volume

        instance = self.tenant_backend.import_instance(self.backend_id, save=True, service_project_link=self.spl)

        self.assertEquals(instance.backend_id, self.backend_id)
        self.assertEquals(models.Volume.objects.count(), 1)
        self.assertEquals(instance.volumes.count(), 1)
        actual_backend_ids = [v.backend_id for v in instance.volumes.all()]
        self.assertItemsEqual([backend_volume.id], actual_backend_ids)

    def test_instance_is_imported_with_attached_volume(self):
        volume_backend_id = 'volume_id'
        backend_volume = self._get_valid_volume(backend_id=volume_backend_id)
        backend_volume.volumeId = backend_volume.id
        self.nova_client_mock.volumes.get_server_volumes.return_value = [backend_volume]
        self.cinder_client_mock.volumes.get.return_value = backend_volume

        instance = self.tenant_backend.import_instance(self.backend_id,
                                                       save=True,
                                                       service_project_link=self.spl)

        self.assertEquals(instance.backend_id, self.backend_id)
        self.assertEquals(models.Volume.objects.count(), 1)
        self.assertEquals(instance.volumes.count(), 1)
        actual_backend_ids = [v.backend_id for v in instance.volumes.all()]
        self.assertItemsEqual([backend_volume.id], actual_backend_ids)

    def test_instance_error_message_is_filled_if_fault_is_provided_by_backend(self):
        expected_error_message = 'An error occurred displaying an error'
        self.backend_instance.fault = dict(message=expected_error_message)
        self.nova_client_mock.volumes.get_server_volumes.return_value = []

        instance = self.tenant_backend.import_instance(self.backend_id,
                                                       save=True,
                                                       service_project_link=self.spl)

        self.assertEquals(instance.backend_id, self.backend_id)
        self.assertEquals(instance.error_message, expected_error_message)
