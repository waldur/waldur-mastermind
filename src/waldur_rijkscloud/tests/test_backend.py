from __future__ import unicode_literals

from django.core.exceptions import ObjectDoesNotExist
from rest_framework import test
from six.moves import mock

from . import factories, fixtures
from .. import models
from ..backend import RijkscloudBackend


class BaseBackendTest(test.APITransactionTestCase):
    def setUp(self):
        super(BaseBackendTest, self).setUp()

        patcher = mock.patch('waldur_rijkscloud.backend.RijkscloudClient')
        patcher.start()

        self.fixture = fixtures.RijkscloudFixture()
        self.backend = RijkscloudBackend(settings=self.fixture.service_settings)

    def tearDown(self):
        super(BaseBackendTest, self).tearDown()
        mock.patch.stopall()


class FlavorPullTest(BaseBackendTest):
    def test_new_flavors_are_created(self):
        self.backend.client.list_flavors.return_value = [
            {
                'name': 'general.8gb',
                'vcpus': 4,
                'ram': 8192
            },
            {
                'name': 'general.4gb',
                'vcpus': 2,
                'ram': 4096
            },
            {
                'name': 'general.2gb',
                'vcpus': 1,
                'ram': 2048
            }
        ]
        self.backend.pull_flavors()
        self.assertEqual(models.Flavor.objects.count(), 3)

    def test_old_flavors_are_removed(self):
        old_flavor = factories.FlavorFactory(settings=self.fixture.service_settings, name='stale')
        self.backend.client.list_flavors.return_value = [
            {
                'name': 'general.8gb',
                'vcpus': 4,
                'ram': 8192
            },
        ]
        self.backend.pull_flavors()
        self.assertEqual(models.Flavor.objects.count(), 1)
        self.assertRaises(ObjectDoesNotExist, old_flavor.refresh_from_db)


class VolumeImportTest(BaseBackendTest):

    def test_existing_volumes_are_skipped(self):
        factories.VolumeFactory(service_project_link=self.fixture.spl, backend_id='stale')
        self.backend.client.list_volumes.return_value = [
            {
                'attachments': [],
                'description': None,
                'metadata': {},
                'name': 'stale',
                'size': 1,
                'status': 'available'
            },
            {
                'attachments': [],
                'description': None,
                'metadata': {},
                'name': 'new',
                'size': 2,
                'status': 'available'
            }
        ]
        volumes = self.backend.get_volumes_for_import()
        self.assertEqual(len(volumes), 1)
        self.assertEqual(volumes[0].backend_id, 'new')

    def test_import_volume(self):
        self.backend.client.get_volume.return_value = {
            'attachments': [],
            'description': None,
            'metadata': {},
            'name': 'test',
            'size': 2,
            'status': 'available'
        }
        volume = self.backend.import_volume('test', service_project_link=self.fixture.spl)
        self.assertEqual(volume.name, 'test')
        self.assertEqual(volume.size, 2048)
        self.assertEqual(volume.runtime_state, 'available')


class FloatingIpPullTest(BaseBackendTest):
    def setUp(self):
        super(FloatingIpPullTest, self).setUp()
        self.backend.client.list_floatingips.return_value = [
            {
                'available': False,
                'float_ip': '123.21.42.121'
            },
            {
                'available': True,
                'float_ip': '97.21.42.121'
            },
        ]

    def test_new_floating_ips_are_created(self):
        self.backend.pull_floating_ips()
        self.assertEqual(models.FloatingIP.objects.count(), 2)

    def test_old_floating_ips_are_removed(self):
        old_fip = factories.FloatingIPFactory(
            settings=self.fixture.service_settings, backend_id='8.8.8.8')
        self.backend.pull_floating_ips()
        self.assertEqual(models.FloatingIP.objects.count(), 2)
        self.assertRaises(ObjectDoesNotExist, old_fip.refresh_from_db)


class NetworkPullTest(BaseBackendTest):
    def setUp(self):
        super(NetworkPullTest, self).setUp()
        self.backend.client.list_networks.return_value = [
            {
                'name': 'service',
                'subnets': [
                    {
                        'name': 'service_subnet',
                        'allocation_pools': [
                            [{'end': '10.10.11.254', 'start': '10.10.11.2'}]
                        ],
                        'cidr': '10.10.11.0/24',
                        'dns_nameservers': [[]],
                        'gateway_ip': ['10.10.11.1'],
                        'ips': [
                            {'available': False, 'ip': '10.10.11.1'},
                            {'available': False, 'ip': '10.10.11.2'},
                            {'available': False, 'ip': '10.10.11.3'},
                            {'available': True, 'ip': '10.10.11.4'},
                            {'available': True, 'ip': '10.10.11.5'},
                        ]
                    }
                ]

            }
        ]

    def test_new_network_is_created(self):
        self.backend.pull_networks()
        self.assertEqual(models.Network.objects.count(), 1)

    def test_gateway_ip_may_be_list_or_string(self):
        self.backend.client.list_networks.return_value[0]['subnets'][0]['gateway_ip'] = '10.10.11.1'
        self.backend.pull_networks()
        self.assertEqual(models.SubNet.objects.count(), 1)
        self.assertEqual(models.SubNet.objects.last().gateway_ip, '10.10.11.1')

    def test_old_network_is_removed(self):
        old_net = factories.NetworkFactory(
            settings=self.fixture.service_settings, backend_id='stale')
        self.backend.pull_networks()
        self.assertEqual(models.Network.objects.count(), 1)
        self.assertRaises(ObjectDoesNotExist, old_net.refresh_from_db)

    def test_new_subnet_is_created(self):
        self.backend.pull_networks()
        self.assertEqual(models.SubNet.objects.count(), 1)

        subnet = models.SubNet.objects.first()
        self.assertEqual(subnet.backend_id, 'service_subnet')
        self.assertEqual(subnet.cidr, '10.10.11.0/24')

    def test_new_internal_ip_is_created(self):
        self.backend.pull_networks()
        self.assertEqual(models.InternalIP.objects.count(), 5)

        internal_ip = models.InternalIP.objects.get(backend_id='10.10.11.1')
        self.assertFalse(internal_ip.is_available)

    def test_existing_internal_ip_is_updated(self):
        network = factories.NetworkFactory(
            settings=self.fixture.service_settings,
            backend_id='service'
        )
        subnet = factories.SubNetFactory(
            settings=self.fixture.service_settings,
            network=network,
            backend_id='service_subnet'
        )
        internal_ip = factories.InternalIPFactory(
            settings=self.fixture.service_settings,
            subnet=subnet,
            backend_id='10.10.11.1',
            is_available=True,
        )
        self.backend.pull_networks()
        internal_ip.refresh_from_db()
        self.assertFalse(internal_ip.is_available)


class InstanceCreateTest(BaseBackendTest):
    def test_request_is_valid(self):
        vm = factories.InstanceFactory(
            service_project_link=self.fixture.spl,
            name='vm01',
            flavor_name='mini',
            floating_ip__address='123.21.42.121',
            internal_ip__address='10.10.11.1',
            internal_ip__subnet__name='int',
            internal_ip__subnet__network__name='service',
        )
        self.backend.create_instance(vm)
        self.backend.client.create_instance.assert_called_once_with({
            'name': 'vm01',
            'flavor': 'mini',
            'userdata': 'normal',
            'interfaces': [
                {
                    'subnets': [
                        {
                            'ip': '10.10.11.1',
                            'name': 'int',
                        }
                    ],
                    'network': 'service',
                    'security_groups': ['any-any'],
                    'float': '123.21.42.121',
                }
            ]
        })


class InstanceImportTest(BaseBackendTest):
    def test_internal_and_floating_ips_are_mapped(self):
        internal_ip = self.fixture.internal_ip
        floating_ip = self.fixture.floating_ip

        self.backend.client.get_instance.return_value = {
            'addresses': [internal_ip.address, floating_ip.address],
            'flavor': 'std.2gb',
            'name': 'test-vm'
        }

        self.backend.client.get_flavor.return_value = {
            'name': 'std.2gb',
            'ram': 2048,
            'vcpus': 1
        }

        instance = self.backend.import_instance('test-vm', service_project_link=self.fixture.spl)
        self.assertEqual(instance.internal_ip, internal_ip)
        self.assertEqual(instance.floating_ip, floating_ip)
