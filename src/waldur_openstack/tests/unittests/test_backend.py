import pickle  # noqa: S403
from unittest import TestCase, mock

from cinderclient import exceptions as cinder_exceptions
from ddt import ddt
from glanceclient import exc as glance_exceptions
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.client import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions
from rest_framework import test

from waldur_mastermind.marketplace_openstack.tests.mocks import MockTenant
from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.tests.fixtures import mock_session

from .. import factories, fixtures


class TestOpenStackBackendError(TestCase):
    def test_reraised_client_exception_is_serializable(self):
        for test_exception in [
            cinder_exceptions.ClientException(404),
            glance_exceptions.ClientException(),
            keystone_exceptions.ClientException(),
            neutron_exceptions.NeutronClientException(),
            nova_exceptions.ClientException(404),
        ]:
            try:
                raise test_exception
            except test_exception.__class__ as e:
                try:
                    raise OpenStackBackendError(e)
                except OpenStackBackendError as e:
                    try:
                        pickle.loads(pickle.dumps(test_exception))  # noqa: S301
                    except Exception as e:
                        self.fail("Reraised exception is not serializable: %s" % str(e))


class BaseBackendTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.mocked_keystone = mock.patch("keystoneclient.v3.client.Client").start()()
        self.mocked_nova = mock.patch("novaclient.v2.client.Client").start()()
        self.mocked_neutron = mock.patch("neutronclient.v2_0.client.Client").start()()
        self.mocked_cinder = mock.patch("cinderclient.v3.client.Client").start()()
        self.mocked_glance = mock.patch("glanceclient.v2.client.Client").start()()

        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.backend = OpenStackBackend(settings=self.fixture.settings)
        mock_session()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()


@ddt
class PullFloatingIPTest(BaseBackendTestCase):
    def _get_valid_new_backend_ip(self, floating_ip=None):
        if floating_ip is None:
            floating_ip = self.fixture.floating_ip
            floating_ip.delete()

        return dict(
            floatingips=[
                {
                    "floating_ip_address": floating_ip.address,
                    "floating_network_id": floating_ip.backend_network_id,
                    "status": floating_ip.runtime_state,
                    "id": floating_ip.backend_id,
                    "description": "",
                    "tenant_id": floating_ip.tenant.backend_id,
                    "port_id": self.fixture.port.backend_id,
                }
            ]
        )

    def setup_client(self, value):
        self.mocked_neutron.list_floatingips.return_value = value

    def call_backend(self):
        return self.backend.pull_tenant_floating_ips(self.fixture.tenant)

    def test_floating_ip_is_created_if_it_does_not_exist(self):
        floating_ip = self.fixture.floating_ip
        floating_ip.backend_network_id = "new_backend_network_id"
        self.setup_client(self._get_valid_new_backend_ip(floating_ip))
        floating_ip.delete()

        self.call_backend()

        self.assertEqual(models.FloatingIP.objects.count(), 1)
        created_ip = models.FloatingIP.objects.get(
            tenant=self.tenant, backend_id=floating_ip.backend_id
        )
        self.assertEqual(created_ip.runtime_state, floating_ip.runtime_state)
        self.assertEqual(created_ip.backend_network_id, floating_ip.backend_network_id)
        self.assertEqual(created_ip.address, floating_ip.address)
        self.assertEqual(created_ip.port, self.fixture.port)

    def test_floating_ip_is_deleted_if_it_is_not_returned_by_neutron(self):
        floating_ip = self.fixture.floating_ip
        self.setup_client(dict(floatingips=[]))

        self.call_backend()

        self.assertRaises(models.FloatingIP.DoesNotExist, floating_ip.refresh_from_db)

    def test_floating_ip_is_not_deleted_if_it_is_in_creating_state(self):
        floating_ip = self.fixture.floating_ip
        floating_ip.state = models.FloatingIP.States.CREATING
        floating_ip.backend_id = ""
        floating_ip.save()
        self.setup_client(dict(floatingips=[]))

        self.call_backend()

        floating_ip.refresh_from_db()

    def test_floating_ip_is_updated(self):
        floating_ip = self.fixture.floating_ip
        floating_ip.runtime_state = "ACTIVE"

        self.setup_client(self._get_valid_new_backend_ip(floating_ip))

        floating_ip.runtime_state = "DOWN"
        floating_ip.save()

        self.call_backend()

        floating_ip.refresh_from_db()
        self.assertEqual(floating_ip.runtime_state, "ACTIVE")
        self.assertEqual(floating_ip.port, self.fixture.port)


@ddt
class PullSecurityGroupsTest(BaseBackendTestCase):
    def setup_client(self, value):
        self.mocked_neutron.list_security_groups.return_value = value

    def call_backend(self):
        return self.backend.pull_tenant_security_groups(self.fixture.tenant)

    def test_missing_security_groups_are_created(self):
        security_group = self.fixture.security_group
        mocked_response = self._form_backend_security_groups([security_group])
        security_group.delete()

        self.setup_client(mocked_response)
        self.call_backend()

        self.assertEqual(models.SecurityGroup.objects.count(), 1)
        new_group = models.SecurityGroup.objects.last()
        self.assertEqual(new_group.tenant, self.fixture.tenant)
        self.assertEqual(new_group.backend_id, security_group.backend_id)
        self.assertEqual(new_group.name, security_group.name)

    def test_stale_security_groups_are_deleted(self):
        security_group = self.fixture.security_group
        self.setup_client(dict(security_groups=[]))

        self.call_backend()

        self.assertRaises(
            models.SecurityGroup.DoesNotExist, security_group.refresh_from_db
        )

    def test_security_groups_are_updated(self):
        security_group = self.fixture.security_group
        security_group.name = "New name"
        security_group.description = "New description"
        security_group.save()
        self.setup_client(self._form_backend_security_groups([security_group]))

        security_group.name = "Old name"
        security_group.description = "Old description"
        security_group.save()

        self.call_backend()
        security_group.refresh_from_db()
        self.assertEqual(security_group.name, "New name")
        self.assertEqual(security_group.description, "New description")

    def test_pending_security_groups_are_not_duplicated(self):
        original_security_group = factories.SecurityGroupFactory(tenant=self.tenant)
        factories.SecurityGroupRuleFactory(security_group=original_security_group)
        security_group_in_progress = factories.SecurityGroupFactory(
            state=models.SecurityGroup.States.UPDATING, tenant=self.tenant
        )
        factories.SecurityGroupRuleFactory(security_group=security_group_in_progress)
        security_groups = [original_security_group, security_group_in_progress]
        self.mocked_neutron.list_security_groups.return_value = (
            self._form_backend_security_groups(security_groups)
        )

        self.backend.pull_tenant_security_groups(self.tenant)

        backend_ids = [sg.backend_id for sg in security_groups]
        actual_security_groups_count = models.SecurityGroup.objects.filter(
            backend_id__in=backend_ids
        ).count()
        self.assertEqual(actual_security_groups_count, len(security_groups))

    def _form_backend_security_groups(self, security_groups):
        result = []

        for security_group in security_groups:
            result.append(
                {
                    "name": security_group.name,
                    "id": security_group.backend_id,
                    "description": security_group.description,
                    "security_group_rules": self._form_backend_security_rules(
                        security_group.rules.all()
                    ),
                    "tenant_id": security_group.tenant.backend_id,
                }
            )

        return {"security_groups": result}

    def _form_backend_security_rules(self, rules):
        result = []

        for rule in rules:
            result.append(
                {
                    "port_range_min": rule.from_port,
                    "port_range_max": rule.to_port,
                    "protocol": rule.protocol,
                    "remote_ip_prefix": rule.cidr,
                    "remote_group_id": None,
                    "description": rule.description,
                    "direction": "ingress",
                    "ethertype": "IPv4",
                    "id": rule.id,
                }
            )

        return result


class PushSecurityGroupTest(BaseBackendTestCase):
    def test_egress_rules_are_modified(self):
        security_group = self.fixture.security_group
        rule = factories.SecurityGroupRuleFactory(security_group=security_group)

        EGRESS_RULE_ID = "93aa42e5-80db-4581-9391-3a608bd0e448"
        INGRESS_RULE_ID = "c0b09f00-1d49-4e64-a0a7-8a186d928138"

        self.mocked_neutron.show_security_group.return_value = {
            "security_group": {
                "security_group_rules": [
                    {
                        "direction": "egress",
                        "id": EGRESS_RULE_ID,
                        "ethertype": "IPv4",
                        "port_range_max": None,
                        "port_range_min": None,
                        "protocol": None,
                        "remote_group_id": None,
                        "remote_ip_prefix": None,
                        "security_group_id": "85cc3048-abc3-43cc-89b3-377341426ac5",
                        "project_id": "e4f50856753b4dc6afee5fa6b9b6c550",
                        "revision_number": 1,
                        "created_at": "2018-03-19T19:16:56Z",
                        "updated_at": "2018-03-19T19:16:56Z",
                        "tenant_id": "e4f50856753b4dc6afee5fa6b9b6c550",
                        "description": "",
                    },
                    {
                        "direction": "ingress",
                        "ethertype": "IPv6",
                        "id": INGRESS_RULE_ID,
                        "port_range_max": None,
                        "port_range_min": None,
                        "protocol": None,
                        "remote_group_id": "85cc3048-abc3-43cc-89b3-377341426ac5",
                        "remote_ip_prefix": None,
                        "security_group_id": "85cc3048-abc3-43cc-89b3-377341426ac5",
                        "project_id": "e4f50856753b4dc6afee5fa6b9b6c550",
                        "revision_number": 2,
                        "created_at": "2018-03-19T19:16:56Z",
                        "updated_at": "2018-03-19T19:16:56Z",
                        "tenant_id": "e4f50856753b4dc6afee5fa6b9b6c550",
                        "description": "",
                    },
                ]
            }
        }

        mocked_security_group_rule = {
            "security_group_id": security_group.backend_id,
            "ethertype": "IPv4",
            "direction": "ingress",
            "protocol": rule.protocol,
            "port_range_min": rule.from_port,
            "port_range_max": rule.to_port,
            "remote_ip_prefix": rule.cidr,
            "remote_group_id": None,
            "description": rule.description,
        }

        self.mocked_neutron.create_security_group_rule.return_value = {
            "security_group_rule": dict(id="valid_id", **mocked_security_group_rule)
        }

        self.backend.push_security_group_rules(security_group)

        self.mocked_neutron.delete_security_group_rule.assert_has_calls(
            [
                mock.call(EGRESS_RULE_ID),
                mock.call(INGRESS_RULE_ID),
            ]
        )
        self.mocked_neutron.create_security_group_rule.assert_called_once_with(
            {"security_group_rule": mocked_security_group_rule}
        )


class PullNetworksTest(BaseBackendTestCase):
    def setUp(self):
        super().setUp()
        self.backend_networks = {
            "networks": [
                {
                    "tenant_id": self.tenant.backend_id,
                    "id": "backend_id",
                    "name": "Private",
                    "description": "Internal network",
                    "router:external": False,
                    "status": "DOWN",
                }
            ]
        }
        self.mocked_neutron.list_networks.return_value = self.backend_networks

    def test_missing_networks_are_created(self):
        self.backend.pull_tenant_networks(self.tenant)

        self.assertEqual(models.Network.objects.count(), 1)
        network = models.Network.objects.get(
            tenant=self.tenant,
            backend_id="backend_id",
        )
        self.assertEqual(network.name, "Private")
        self.assertEqual(network.description, "Internal network")

    def test_stale_networks_are_deleted(self):
        self.fixture.network
        self.mocked_neutron.list_networks.return_value = dict(networks=[])
        self.backend.pull_tenant_networks(self.tenant)
        self.assertEqual(models.Network.objects.count(), 0)

    def test_existing_networks_are_updated(self):
        network = factories.NetworkFactory(
            tenant=self.tenant,
            backend_id="backend_id",
            name="Old name",
        )
        self.backend.pull_tenant_networks(self.tenant)
        network.refresh_from_db()
        self.assertEqual(network.name, "Private")


class PullSubnetsTest(BaseBackendTestCase):
    def setUp(self):
        super().setUp()
        self.network = factories.NetworkFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.tenant,
            backend_id="network_id",
        )
        self.backend_subnets = {
            "subnets": [
                {
                    "id": "backend_id",
                    "network_id": "network_id",
                    "name": "subnet-1",
                    "description": "",
                    "cidr": "192.168.42.0/24",
                    "enable_dhcp": False,
                    "gateway_ip": "192.168.42.1",
                    "dns_nameservers": ["8.8.8.8"],
                    "ip_version": 4,
                    "allocation_pools": [
                        {
                            "start": "192.168.42.10",
                            "end": "192.168.42.100",
                        }
                    ],
                }
            ]
        }
        self.mocked_neutron.list_subnets.return_value = self.backend_subnets

    def test_missing_subnets_are_created(self):
        self.backend.pull_subnets()

        self.mocked_neutron.list_subnets.assert_called_once()
        self.assertEqual(models.SubNet.objects.count(), 1)
        subnet = models.SubNet.objects.get(
            backend_id="backend_id",
            network=self.network,
        )
        self.assertEqual(subnet.name, "subnet-1")
        self.assertEqual(subnet.cidr, "192.168.42.0/24")
        self.assertEqual(
            subnet.allocation_pools,
            [
                {
                    "start": "192.168.42.10",
                    "end": "192.168.42.100",
                }
            ],
        )

    def test_subnet_is_not_pulled_if_network_is_not_pulled_yet(self):
        self.network.delete()
        self.backend.pull_subnets()
        self.assertEqual(models.SubNet.objects.count(), 0)

    def test_stale_subnets_are_deleted(self):
        self.fixture.subnet
        self.assertEqual(models.SubNet.objects.count(), 1)
        self.mocked_neutron.list_subnets.return_value = dict(subnets=[])
        self.backend.pull_subnets()
        self.assertEqual(models.SubNet.objects.count(), 0)

    def test_existing_subnets_are_updated(self):
        subnet = factories.SubNetFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            backend_id="backend_id",
            name="Old name",
            network=self.network,
        )
        self.backend.pull_subnets()
        subnet.refresh_from_db()
        self.assertEqual(subnet.name, "subnet-1")


class CreateOrUpdateTenantUserTest(BaseBackendTestCase):
    def test_change_tenant_user_password_is_called_if_user_exists(self):
        self.mocked_keystone.users.find.return_value = self.fixture.owner

        self.backend.create_or_update_tenant_user(self.tenant)

        self.mocked_keystone.users.update.assert_called_once()

    def test_user_is_created_if_it_is_not_found(self):
        self.mocked_keystone.users.find.side_effect = keystone_exceptions.NotFound

        self.backend.create_or_update_tenant_user(self.tenant)

        self.mocked_keystone.users.create.assert_called_once()


class ImportTenantNetworksTest(BaseBackendTestCase):
    def _generate_backend_networks(self, count=1):
        networks = []

        for i in range(0, count):
            networks.append(
                {
                    "name": "network_%s" % i,
                    "tenant_id": self.tenant.backend_id,
                    "description": "network_description_%s" % i,
                    "router:external": True,
                    "status": "ONLINE",
                    "id": "backend_id_%s" % i,
                }
            )

        return networks

    def test_import_tenant_networks_imports_network(self):
        backend_network = self._generate_backend_networks()[0]
        self.mocked_neutron.list_networks.return_value = {"networks": [backend_network]}
        self.assertEqual(self.tenant.networks.count(), 0)

        self.backend.import_tenant_networks(self.tenant)

        self.assertEqual(self.tenant.networks.count(), 1)
        network = self.tenant.networks.first()
        self.assertEqual(network.name, backend_network["name"])
        self.assertEqual(network.description, backend_network["description"])
        self.assertEqual(network.is_external, backend_network["router:external"])
        self.assertEqual(network.backend_id, backend_network["id"])
        self.assertEqual(network.tenant.id, self.tenant.id)
        self.assertEqual(self.tenant.internal_network_id, backend_network["id"])

    def test_internal_network_is_not_set_if_networks_are_missing(self):
        self.mocked_neutron.list_networks.return_value = {"networks": []}

        self.backend.import_tenant_networks(self.tenant)

        self.assertEqual(self.tenant.networks.count(), 0)
        self.assertEqual(self.tenant.internal_network_id, "")

    def test_networks_are_updated_if_they_exist(self):
        backend_network = self._generate_backend_networks()[0]
        self.mocked_neutron.list_networks.return_value = {"networks": [backend_network]}
        network = factories.NetworkFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id=backend_network["id"],
        )
        self.assertEqual(self.tenant.networks.count(), 1)

        self.backend.import_tenant_networks(self.tenant)

        self.assertEqual(self.tenant.networks.count(), 1)
        network.refresh_from_db()
        self.assertEqual(network.name, backend_network["name"])
        self.assertEqual(network.description, backend_network["description"])
        self.assertEqual(network.is_external, backend_network["router:external"])
        self.assertEqual(network.backend_id, backend_network["id"])
        self.assertEqual(network.tenant.id, self.tenant.id)
        self.assertEqual(self.tenant.internal_network_id, backend_network["id"])


class ImportTenantSubnets(BaseBackendTestCase):
    def setUp(self):
        super().setUp()
        self.network = self.fixture.network

    def _generate_backend_subnet(self, count=1):
        subnets = []

        for i in range(0, count):
            subnets.append(
                {
                    "name": "network_%s" % i,
                    "tenant_id": self.tenant.backend_id,
                    "description": "network_description_%s" % i,
                    "allocation_pools": [],
                    "cidr": "24",
                    "ip_version": 4,
                    "enable_dhcp": True,
                    "gateway_ip": "127.0.0.1",
                    "network_id": self.network.backend_id,
                    "dns_nameservers": "waldur.example",
                    "id": self.network.backend_id,
                }
            )

        return subnets

    def test_tenant_subnet_is_imported(self):
        backend_subnet = self._generate_backend_subnet()[0]
        self.mocked_neutron.list_subnets.return_value = {"subnets": [backend_subnet]}
        self.assertEqual(models.SubNet.objects.count(), 0)

        self.backend.pull_tenant_subnets(self.tenant)

        self.assertEqual(models.SubNet.objects.count(), 1)
        subnet = models.SubNet.objects.get(
            network=self.network, backend_id=backend_subnet["id"]
        )
        self.assertEqual(subnet.name, backend_subnet["name"])
        self.assertEqual(subnet.description, backend_subnet["description"])
        self.assertEqual(subnet.cidr, backend_subnet["cidr"])
        self.assertEqual(subnet.ip_version, backend_subnet["ip_version"])
        self.assertEqual(subnet.enable_dhcp, backend_subnet["enable_dhcp"])
        self.assertEqual(subnet.dns_nameservers, backend_subnet["dns_nameservers"])

    def test_tenant_subnets_are_not_imported_if_network_is_missing(self):
        backend_subnet = self._generate_backend_subnet()[0]
        self.tenant.networks.all().delete()
        self.mocked_neutron.list_subnets.return_value = {"subnets": [backend_subnet]}
        self.assertEqual(models.SubNet.objects.count(), 0)

        self.backend.pull_tenant_subnets(self.tenant)

        self.assertEqual(models.SubNet.objects.count(), 0)


class CreateTenantTest(BaseBackendTestCase):
    def test_name_is_replaced_if_it_is_already_taken(self):
        self.tenant.name = "First Tenant"
        self.tenant.save()
        self.mocked_keystone.projects.list.return_value = [
            MockTenant("First Tenant"),
            MockTenant("Second Tenant"),
        ]
        self.mocked_keystone.projects.create.return_value = MockTenant(
            "First Tenant", "VALID_ID"
        )
        self.backend.create_tenant_safe(self.tenant)
        self.tenant.refresh_from_db()
        self.assertNotEqual(self.tenant.name, "First Tenant")
        self.assertTrue(self.tenant.name.startswith("First Tenant"))

    def test_name_is_not_replaced_if_it_is_not_taken(self):
        self.tenant.name = "First Tenant"
        self.tenant.save()
        self.mocked_keystone.projects.list.return_value = []
        self.mocked_keystone.projects.create.return_value = MockTenant(
            "First Tenant", "VALID_ID"
        )
        self.backend.create_tenant_safe(self.tenant)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.name, "First Tenant")


class PullImagesTest(BaseBackendTestCase):
    def setUp(self):
        super().setUp()
        self.mocked_glance.images.list.return_value = [
            {
                "status": "active",
                "id": "1",
                "name": "CentOS 7",
                "min_ram": 1024,
                "min_disk": 10,
                "visibility": "public",
            }
        ]

    def test_new_image_is_added(self):
        self.backend.pull_tenant_images(self.fixture.tenant)
        image = models.Image.objects.filter(
            backend_id="1",
            name="CentOS 7",
            min_ram=1024,
            min_disk=10240,
            settings=self.fixture.settings,
        )
        self.assertEqual(models.Image.objects.count(), 1)
        self.assertTrue(image.exists())

    def test_old_image_is_deleted(self):
        models.Image.objects.create(
            backend_id="2",
            name="CentOS 6",
            min_ram=2048,
            min_disk=10240,
            settings=self.fixture.settings,
        )
        self.backend.pull_tenant_images(self.fixture.tenant)
        self.assertEqual(self.tenant.images.count(), 1)
        self.assertFalse(self.tenant.images.filter(backend_id="2").exists())

    def test_existing_image_is_updated(self):
        models.Image.objects.create(
            backend_id="1",
            name="CentOS 7",
            min_ram=2048,
            min_disk=10240,
            settings=self.fixture.settings,
        )
        self.backend.pull_tenant_images(self.fixture.tenant)
        self.assertEqual(models.Image.objects.count(), 1)
        self.assertEqual(models.Image.objects.get(backend_id="1").min_ram, 1024)

    def test_deleted_images_are_filtered_out(self):
        self.mocked_glance.images.list.return_value[0]["status"] = "deleted"
        self.backend.pull_tenant_images(self.fixture.tenant)
        self.assertEqual(models.Image.objects.count(), 0)


class PullPortsTest(BaseBackendTestCase):
    def setUp(self):
        super().setUp()
        self.subnet = self.fixture.subnet
        self.subnet.backend_id = f"{self.subnet.name}_backend_id"
        self.subnet.save()

        self.port: models.Port = self.fixture.port
        self.port.backend_id = f"{self.port.name}_backend_id"
        self.port.fixed_ips = [
            {"ip_address": "192.168.11.1", "subnet_id": self.subnet.backend_id}
        ]
        self.port.save()

    def _get_valid_new_backend_port(self, **kwargs):
        port = self.port
        return dict(
            ports=[
                {
                    "id": port.backend_id,
                    "name": port.name,
                    "tenant_id": port.tenant.backend_id,
                    "network_id": port.network.backend_id,
                    "fixed_ips": port.fixed_ips,
                    "description": port.description,
                    "mac_address": port.mac_address,
                    "security_groups": [],
                    "port_security_enabled": True,
                    **kwargs,
                }
            ]
        )

    def setup_client(self, value):
        self.mocked_neutron.list_ports.return_value = value

    def call_backend(self):
        return self.backend.pull_tenant_ports(self.tenant)

    def test_port_is_created_if_does_not_exists(self):
        port = self.port
        self.setup_client(self._get_valid_new_backend_port())
        port.delete()

        self.call_backend()

        self.assertEqual(models.Port.objects.count(), 1)
        created_port: models.Port = models.Port.objects.get(
            tenant=self.tenant, backend_id=port.backend_id
        )

        self.assertEqual(created_port.state, models.Port.States.OK)
        self.assertEqual(created_port.network, port.network)

        self.assertEqual(
            created_port.fixed_ips,
            [{"ip_address": "192.168.11.1", "subnet_id": self.subnet.backend_id}],
        )

    def test_port_is_deleted_if_it_is_not_returned_by_neutron(self):
        port = self.port
        self.setup_client(dict(ports=[]))

        self.call_backend()

        self.assertRaises(models.Port.DoesNotExist, port.refresh_from_db)

    def test_port_is_updated(self):
        port: models.Port = self.port

        self.setup_client(self._get_valid_new_backend_port())

        port.fixed_ips = []
        port.save()

        self.call_backend()

        port.refresh_from_db()

        self.assertEqual(
            port.fixed_ips,
            [{"ip_address": "192.168.11.1", "subnet_id": self.subnet.backend_id}],
        )

    def test_missing_security_groups_are_attached(self):
        security_group = self.fixture.security_group
        self.setup_client(
            self._get_valid_new_backend_port(
                security_groups=[security_group.backend_id]
            )
        )

        self.call_backend()

        self.port.refresh_from_db()
        self.assertEqual(security_group, self.port.security_groups.get())

    def test_stale_security_groups_are_detached(self):
        self.port.security_groups.add(self.fixture.security_group)
        self.setup_client(self._get_valid_new_backend_port(security_groups=[]))

        self.call_backend()

        self.port.refresh_from_db()
        self.assertEqual(0, self.port.security_groups.count())


@ddt
class PullServerGroupsTest(BaseBackendTestCase):
    def call_backend(self):
        return self.backend.pull_tenant_server_groups(self.fixture.tenant)

    def test_missing_server_groups_are_created(self):
        mock_server_group = mock.Mock()
        mock_server_group.name = "mock_server_group"
        mock_server_group.policies = ["affinity"]
        mock_server_group.id = self.tenant.backend_id
        mock_server_group.project_id = self.tenant.backend_id

        self.mocked_nova.server_groups.list.return_value = [mock_server_group]

        self.assertFalse(
            models.ServerGroup.objects.filter(
                tenant=self.tenant,
                backend_id=self.tenant.backend_id,
                name=mock_server_group.name,
            ).exists()
        )

        self.call_backend()

        self.assertTrue(
            models.ServerGroup.objects.filter(
                tenant=self.tenant,
                backend_id=self.tenant.backend_id,
                name=mock_server_group.name,
            ).exists()
        )

    def test_stale_server_groups_are_deleted(self):
        server_group = self.fixture.server_group
        self.mocked_nova.server_groups.list.return_value = []

        self.call_backend()

        self.assertRaises(models.ServerGroup.DoesNotExist, server_group.refresh_from_db)
