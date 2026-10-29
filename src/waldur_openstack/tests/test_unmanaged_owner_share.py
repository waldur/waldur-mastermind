"""Networks shared with a tenant by an OpenStack project Waldur does not manage.

The typical owner is the cloud's admin or service project. It has no tenant in
Waldur, so the share is discovered from its RBAC policy and the owner is kept as
an unmanaged tenant that Waldur only reads.
"""

import copy
from unittest import mock

from django.test import TestCase
from neutronclient.common import exceptions as neutron_exceptions
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.utils import handle_resource_not_found
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OPENSTACK_TENANT_OFFERING
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack import executors, models, tasks
from waldur_openstack.backend import (
    OpenStackBackend,
    OpenStackBackendError,
    get_tenant_session,
)
from waldur_openstack.extension import OpenStackExtension
from waldur_openstack.tests.fixtures import mock_session

from . import factories, fixtures

OWNER_ID = "admin-project-id"
NETWORK_ID = "provider-net"
SUBNET_ID = "provider-subnet"
POLICY_ID = "policy-1"

BACKEND_NETWORK = {
    "id": NETWORK_ID,
    "name": "provider-lan",
    "description": "",
    "router:external": False,
    "status": "ACTIVE",
    "mtu": 1500,
    "project_id": OWNER_ID,
    "tenant_id": OWNER_ID,
}

BACKEND_SUBNET = {
    "id": SUBNET_ID,
    "name": "provider-lan-subnet",
    "description": "",
    "network_id": NETWORK_ID,
    "cidr": "10.97.0.0/24",
    "ip_version": 4,
    "enable_dhcp": True,
    "gateway_ip": "10.97.0.1",
    "dns_nameservers": [],
    "allocation_pools": [{"start": "10.97.0.2", "end": "10.97.0.254"}],
}


def policy(policy_id=POLICY_ID, target=None, object_id=NETWORK_ID, action=None):
    return {
        "id": policy_id,
        "object_type": "network",
        "object_id": object_id,
        "target_tenant": target,
        "action": action or "access_as_shared",
    }


class UnmanagedOwnerShareMixin:
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.OpenStackFixture()
        self.settings = self.fixture.settings
        # The consumer lives in another organization than the provider, so that
        # nothing it sees comes from sharing a customer with the settings.
        self.consumer_project = structure_factories.ProjectFactory()
        self.consumer = factories.TenantFactory(
            service_settings=self.settings,
            project=self.consumer_project,
            backend_id="consumer-project-id",
            state=CoreStates.OK,
        )
        self.backend = OpenStackBackend(self.settings)

        mock_session()
        self.neutron = (
            mock.patch("waldur_openstack.backend.get_neutron_client")
            .start()
            .return_value
        )
        self.keystone = (
            mock.patch("waldur_openstack.backend.get_keystone_client")
            .start()
            .return_value
        )
        self.addCleanup(mock.patch.stopall)
        self.keystone.projects.get.return_value.name = "admin"

        self.backend_network = copy.deepcopy(BACKEND_NETWORK)
        self.neutron.show_network.side_effect = lambda network_id: {
            "network": self.backend_network
        }
        self.neutron.list_subnets.return_value = {"subnets": [BACKEND_SUBNET]}
        self.neutron.list_ports.return_value = {"ports": []}
        self.share([policy(target=self.consumer.backend_id)])

    def share(self, policies):
        self.neutron.list_rbac_policies.return_value = {"rbac_policies": policies}

    def pull(self, tenant=None):
        self.backend.pull_tenant_network_rbac_policies(tenant or self.consumer)

    def owner(self):
        return models.Tenant.objects.get(
            service_settings=self.settings, backend_id=OWNER_ID
        )


class ImportTest(UnmanagedOwnerShareMixin, TestCase):
    def test_share_is_imported_under_an_unmanaged_owner(self):
        self.pull()

        owner = self.owner()
        self.assertFalse(owner.is_managed)
        self.assertEqual(owner.state, CoreStates.OK)
        self.assertEqual(owner.name, "admin")
        self.assertEqual(owner.user_username, "")
        self.assertEqual(owner.project.customer, self.settings.customer)

        network = models.Network.objects.get(backend_id=NETWORK_ID)
        self.assertEqual(network.tenant, owner)
        self.assertEqual(network.project, owner.project)
        subnet = models.SubNet.objects.get(backend_id=SUBNET_ID)
        self.assertEqual(subnet.network, network)
        self.assertEqual(subnet.tenant, owner)

        rbac = models.NetworkRBACPolicy.objects.get(backend_id=POLICY_ID)
        self.assertEqual(rbac.network, network)
        self.assertEqual(rbac.target_tenant, self.consumer)
        self.assertIn(subnet, self.consumer.available_subnets)

    def test_unmanaged_owner_gets_no_marketplace_footprint(self):
        self.pull()

        owner = self.owner()
        self.assertFalse(
            marketplace_models.Resource.objects.filter(object_id=owner.id).exists()
        )
        self.assertFalse(
            marketplace_models.Offering.objects.filter(object_id=owner.id).exists()
        )

    def test_repeated_pull_does_not_duplicate(self):
        self.pull()

        self.pull()

        self.assertEqual(models.Tenant.objects.filter(is_managed=False).count(), 1)
        self.assertEqual(
            models.Network.objects.filter(backend_id=NETWORK_ID).count(), 1
        )
        self.assertEqual(models.SubNet.objects.filter(backend_id=SUBNET_ID).count(), 1)

    def test_owners_of_one_settings_share_one_project(self):
        other_consumer = factories.TenantFactory(
            service_settings=self.settings, backend_id="other-consumer-id"
        )
        self.pull()
        self.backend_network = dict(
            copy.deepcopy(BACKEND_NETWORK),
            id="service-net",
            project_id="service-project-id",
            tenant_id="service-project-id",
        )
        self.neutron.list_subnets.return_value = {"subnets": []}
        self.share([policy("policy-2", other_consumer.backend_id, "service-net")])

        self.pull(other_consumer)

        owners = models.Tenant.objects.filter(is_managed=False)
        self.assertEqual(owners.count(), 2)
        self.assertEqual(len({owner.project_id for owner in owners}), 1)

    def test_owner_project_is_reused_after_the_owner_was_dropped(self):
        self.pull()
        project = self.owner().project
        self.share([])
        self.pull()
        self.assertFalse(models.Tenant.objects.filter(is_managed=False).exists())

        self.share([policy(target=self.consumer.backend_id)])
        self.pull()

        self.assertEqual(self.owner().project, project)

    def test_share_from_a_managed_tenant_is_left_to_its_own_pull(self):
        factories.TenantFactory(service_settings=self.settings, backend_id=OWNER_ID)

        self.pull()

        self.assertFalse(models.Network.objects.filter(backend_id=NETWORK_ID).exists())
        self.assertFalse(models.Tenant.objects.filter(is_managed=False).exists())

    def test_external_share_is_not_imported(self):
        self.share(
            [policy(target=self.consumer.backend_id, action="access_as_external")]
        )

        self.pull()

        self.assertFalse(models.Network.objects.filter(backend_id=NETWORK_ID).exists())
        self.neutron.show_network.assert_not_called()

    def test_settings_without_an_organization_skip_the_share(self):
        self.settings.customer = None
        self.settings.save()

        self.pull()

        self.assertFalse(models.Network.objects.filter(backend_id=NETWORK_ID).exists())
        self.assertFalse(models.Tenant.objects.filter(is_managed=False).exists())

    def test_settings_without_an_organization_use_their_provider(self):
        provider = structure_factories.CustomerFactory()
        marketplace_factories.OfferingFactory(
            scope=self.settings, type=OPENSTACK_TENANT_OFFERING, customer=provider
        )
        self.settings.customer = None
        self.settings.save()

        self.pull()

        self.assertEqual(self.owner().project.customer, provider)

    def test_tenant_offerings_of_different_providers_skip_the_share(self):
        for _ in range(2):
            marketplace_factories.OfferingFactory(
                scope=self.settings, type=OPENSTACK_TENANT_OFFERING
            )
        self.settings.customer = None
        self.settings.save()

        self.pull()

        self.assertFalse(models.Tenant.objects.filter(is_managed=False).exists())

    def test_archived_tenant_offering_of_another_provider_is_ignored(self):
        provider = structure_factories.CustomerFactory()
        marketplace_factories.OfferingFactory(
            scope=self.settings, type=OPENSTACK_TENANT_OFFERING, customer=provider
        )
        marketplace_factories.OfferingFactory(
            scope=self.settings,
            type=OPENSTACK_TENANT_OFFERING,
            state=marketplace_models.Offering.States.ARCHIVED,
        )
        self.settings.customer = None
        self.settings.save()

        self.pull()

        self.assertEqual(self.owner().project.customer, provider)

    def test_vanished_network_is_skipped(self):
        self.neutron.show_network.side_effect = neutron_exceptions.NotFound()

        self.pull()

        self.assertFalse(models.Tenant.objects.filter(is_managed=False).exists())

    def test_owned_network_listing_does_not_import_or_reparent_it(self):
        """Some listings (the emulator's) return shares as well; they stay put."""
        self.pull()
        with mock.patch.object(
            OpenStackBackend, "list_networks", return_value=[self.backend_network]
        ):
            self.backend.pull_tenant_networks(self.consumer)

        network = models.Network.objects.get(backend_id=NETWORK_ID)
        self.assertEqual(network.tenant, self.owner())
        self.assertFalse(self.consumer.networks.exists())


class CleanupTest(UnmanagedOwnerShareMixin, TestCase):
    def test_revoked_share_drops_network_and_owner_locally_only(self):
        self.pull()
        self.share([])

        self.pull()

        self.assertFalse(models.Network.objects.filter(backend_id=NETWORK_ID).exists())
        self.assertFalse(models.SubNet.objects.filter(backend_id=SUBNET_ID).exists())
        self.assertFalse(models.Tenant.objects.filter(is_managed=False).exists())
        self.neutron.delete_network.assert_not_called()
        self.neutron.delete_subnet.assert_not_called()
        self.keystone.projects.delete.assert_not_called()

    def test_deleting_the_last_consumer_drops_network_and_owner(self):
        """No pull of the consumer runs again, so the deletion has to sweep."""
        self.pull()

        with self.captureOnCommitCallbacks(execute=True):
            self.consumer.delete()

        self.assertFalse(models.Network.objects.filter(backend_id=NETWORK_ID).exists())
        self.assertFalse(models.Tenant.objects.filter(is_managed=False).exists())
        self.neutron.delete_network.assert_not_called()

    def test_network_with_consumer_ports_is_kept(self):
        self.pull()
        network = models.Network.objects.get(backend_id=NETWORK_ID)
        port = factories.PortFactory(
            network=network,
            tenant=self.consumer,
            service_settings=self.settings,
            project=self.consumer_project,
        )
        self.share([])

        self.pull()

        self.assertTrue(models.Network.objects.filter(pk=network.pk).exists())
        self.assertTrue(models.Port.objects.filter(pk=port.pk).exists())

    def test_network_still_shared_with_another_tenant_is_kept(self):
        other_consumer = factories.TenantFactory(
            service_settings=self.settings, backend_id="other-consumer-id"
        )
        both = [
            policy(target=self.consumer.backend_id),
            policy("policy-2", other_consumer.backend_id),
        ]
        self.share(both)
        self.pull()
        self.pull(other_consumer)

        self.share([both[1]])
        self.pull()

        network = models.Network.objects.get(backend_id=NETWORK_ID)
        self.assertEqual(
            list(network.rbac_policies.values_list("target_tenant", flat=True)),
            [other_consumer.id],
        )
        self.assertTrue(self.owner().is_managed is False)


class LifecycleTest(UnmanagedOwnerShareMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.pull()

    def test_no_tenant_session_is_opened_for_it(self):
        with self.assertRaises(OpenStackBackendError):
            get_tenant_session(self.owner())

    def test_periodic_tenant_pulls_skip_it(self):
        for task in (
            tasks.TenantResourcesListPullTask,
            tasks.TenantSubresourcesListPullTask,
            tasks.TenantPropertiesListPullTask,
        ):
            pulled = task().get_pulled_objects()
            self.assertIn(self.consumer, pulled)
            self.assertNotIn(self.owner(), pulled)

    @mock.patch("waldur_openstack.executors.TenantPullQuotasExecutor.execute")
    def test_quota_pull_skips_it(self, execute):
        tasks.TenantPullQuotas().run()

        pulled = [call.args[0] for call in execute.call_args_list]
        self.assertIn(self.consumer, pulled)
        self.assertNotIn(self.owner(), pulled)


class ApiTest(UnmanagedOwnerShareMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.pull()
        self.network = models.Network.objects.get(backend_id=NETWORK_ID)
        self.subnet = models.SubNet.objects.get(backend_id=SUBNET_ID)
        self.consumer_admin = structure_factories.UserFactory()
        self.consumer_project.add_user(self.consumer_admin, ProjectRole.ADMIN)
        self.staff = structure_factories.UserFactory(is_staff=True)

    def list_for(self, user, factory, **params):
        self.client.force_authenticate(user)
        response = self.client.get(factory.get_list_url(), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_consumer_sees_the_network_and_subnet(self):
        tenant_filter = {"tenant_uuid": self.consumer.uuid.hex}

        networks = self.list_for(
            self.consumer_admin, factories.NetworkFactory, **tenant_filter
        )
        subnets = self.list_for(
            self.consumer_admin, factories.SubNetFactory, **tenant_filter
        )

        self.assertEqual([n["backend_id"] for n in networks], [NETWORK_ID])
        self.assertFalse(networks[0]["tenant_is_managed"])
        self.assertEqual([s["backend_id"] for s in subnets], [SUBNET_ID])
        # The subnet says so too, so its actions can be explained as well.
        self.assertFalse(subnets[0]["tenant_is_managed"])

    def test_an_own_subnet_is_managed(self):
        own = factories.SubNetFactory(
            network=factories.NetworkFactory(
                tenant=self.consumer,
                service_settings=self.settings,
                project=self.consumer_project,
            ),
            tenant=self.consumer,
            service_settings=self.settings,
            project=self.consumer_project,
        )
        self.client.force_authenticate(self.consumer_admin)

        response = self.client.get(factories.SubNetFactory.get_url(own))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["tenant_is_managed"])

    def test_consumer_does_not_see_the_owner_tenant(self):
        tenants = self.list_for(self.consumer_admin, factories.TenantFactory)

        self.assertEqual([t["uuid"] for t in tenants], [self.consumer.uuid.hex])

    def test_outsider_sees_neither(self):
        outsider = structure_factories.UserFactory()

        self.assertEqual(self.list_for(outsider, factories.NetworkFactory), [])
        self.assertEqual(self.list_for(outsider, factories.SubNetFactory), [])
        self.assertEqual(self.list_for(outsider, factories.TenantFactory), [])

    def test_staff_sees_the_owner_as_unmanaged(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(factories.TenantFactory.get_url(self.owner()))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_managed"])

    def assert_refused(self, method, url, data=None):
        self.client.force_authenticate(self.staff)
        response = getattr(self.client, method)(url, data or {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not managed by Waldur", str(response.data))

    def test_owner_tenant_cannot_be_changed(self):
        owner = self.owner()
        self.assert_refused(
            "patch", factories.TenantFactory.get_url(owner), {"name": "x"}
        )
        self.assert_refused("post", factories.TenantFactory.get_url(owner, "pull"))
        self.assert_refused(
            "post", factories.TenantFactory.get_url(owner, "set_quotas"), {"vcpu": 1}
        )
        self.assert_refused(
            "post",
            factories.TenantFactory.get_url(owner, "create_network"),
            {"name": "x"},
        )

    def test_its_network_and_subnet_cannot_be_changed(self):
        self.assert_refused(
            "patch", factories.NetworkFactory.get_url(self.network), {"name": "x"}
        )
        self.assert_refused("delete", factories.NetworkFactory.get_url(self.network))
        self.assert_refused(
            "post", factories.NetworkFactory.get_url(self.network, "pull")
        )
        self.assert_refused(
            "patch", factories.SubNetFactory.get_url(self.subnet), {"name": "x"}
        )
        self.assert_refused("delete", factories.SubNetFactory.get_url(self.subnet))

    def test_its_sharing_cannot_be_changed_from_waldur(self):
        other_consumer = factories.TenantFactory(service_settings=self.settings)
        self.client.force_authenticate(self.staff)

        response = self.client.post(
            "/api/openstack-network-rbac-policies/",
            {
                "network": factories.NetworkFactory.get_url(self.network),
                "target_tenant": factories.TenantFactory.get_url(other_consumer),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("network", response.data)

        rbac = models.NetworkRBACPolicy.objects.get(backend_id=POLICY_ID)
        response = self.client.delete(
            f"/api/openstack-network-rbac-policies/{rbac.uuid.hex}/"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.neutron.delete_rbac_policy.assert_not_called()
        self.assertTrue(models.NetworkRBACPolicy.objects.filter(pk=rbac.pk).exists())

    def test_it_cannot_be_the_target_of_a_share(self):
        own_network = factories.NetworkFactory(
            tenant=self.consumer,
            service_settings=self.settings,
            project=self.consumer_project,
        )
        self.client.force_authenticate(self.staff)

        response = self.client.post(
            "/api/openstack-network-rbac-policies/",
            {
                "network": factories.NetworkFactory.get_url(own_network),
                "target_tenant": factories.TenantFactory.get_url(self.owner()),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("target_tenant", response.data)

    @mock.patch("waldur_openstack.executors.PortCreateExecutor.execute")
    def test_consumer_creates_a_port_on_it(self, execute):
        self.client.force_authenticate(self.consumer_admin)

        response = self.client.post(
            factories.PortFactory.get_list_url(),
            {
                "name": "port-on-provider-lan",
                "network": factories.NetworkFactory.get_url(self.network),
                "target_tenant": factories.TenantFactory.get_url(self.consumer),
                "fixed_ips": [{"subnet_id": SUBNET_ID}],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        port = models.Port.objects.get(uuid=response.data["uuid"])
        self.assertEqual(port.tenant, self.consumer)
        self.assertEqual(port.project, self.consumer_project)

    @mock.patch("waldur_openstack.executors.PortCreateExecutor.execute")
    def test_port_must_name_its_tenant(self, execute):
        self.client.force_authenticate(self.staff)

        response = self.client.post(
            factories.PortFactory.get_list_url(),
            {
                "name": "port-without-tenant",
                "network": factories.NetworkFactory.get_url(self.network),
                "fixed_ips": [{"subnet_id": SUBNET_ID}],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("target_tenant", response.data)
        execute.assert_not_called()

    # Neutron leaves these to the network's owner; Waldur calls it as admin, so
    # without its own check a tenant could spoof the others on the segment.

    @mock.patch("waldur_openstack.executors.PortCreateExecutor.execute")
    def test_consumer_cannot_choose_owner_only_settings_of_a_port_on_it(self, execute):
        self.client.force_authenticate(self.consumer_admin)
        port = {
            "name": "port-on-provider-lan",
            "network": factories.NetworkFactory.get_url(self.network),
            "target_tenant": factories.TenantFactory.get_url(self.consumer),
            "fixed_ips": [{"subnet_id": SUBNET_ID}],
        }
        for owner_only in (
            {"port_security_enabled": False},
            {"mac_address": "fa:16:3e:00:00:01"},
            {"fixed_ips": [{"subnet_id": SUBNET_ID, "ip_address": "10.97.0.50"}]},
        ):
            with self.subTest(owner_only=owner_only):
                response = self.client.post(
                    factories.PortFactory.get_list_url(), {**port, **owner_only}
                )
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
        execute.assert_not_called()

    def consumer_port(self):
        return factories.PortFactory(
            network=self.network,
            subnet=self.subnet,
            tenant=self.consumer,
            service_settings=self.settings,
            project=self.consumer_project,
            state=CoreStates.OK,
        )

    @mock.patch.object(OpenStackBackend, "disable_port_security")
    def test_consumer_cannot_disable_port_security_on_it(self, disable):
        port = self.consumer_port()
        url = factories.PortFactory.get_url(port, action="disable_port_security")

        self.client.force_authenticate(self.consumer_admin)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        disable.assert_not_called()

        self.client.force_authenticate(self.staff)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        disable.assert_called_once()

    @mock.patch.object(OpenStackBackend, "update_port_ip")
    def test_consumer_cannot_choose_the_address_of_its_port_on_it(self, update):
        port = self.consumer_port()

        self.client.force_authenticate(self.consumer_admin)
        response = self.client.post(
            factories.PortFactory.get_url(port, action="update_port_ip"),
            {
                "subnet": factories.SubNetFactory.get_url(self.subnet),
                "ip_address": "10.97.0.50",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        update.assert_not_called()

    def test_consumer_sees_only_its_own_share_of_it(self):
        """Every tenant of the cloud may have a share of a provider network."""
        other_customer_tenant = factories.TenantFactory(
            service_settings=self.settings, backend_id="other-customer-tenant"
        )
        models.NetworkRBACPolicy.objects.create(
            network=self.network,
            target_tenant=other_customer_tenant,
            backend_id=POLICY_ID,
            policy_type="access_as_shared",
        )

        def share_targets(user):
            self.client.force_authenticate(user)
            response = self.client.get(factories.NetworkFactory.get_url(self.network))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            return {
                policy["target_tenant_name"]
                for policy in response.data["rbac_policies"]
            }

        self.assertEqual(share_targets(self.consumer_admin), {self.consumer.name})
        self.assertEqual(
            share_targets(self.staff),
            {self.consumer.name, other_customer_tenant.name},
        )


class ProvisionedProjectTest(UnmanagedOwnerShareMixin, TestCase):
    def test_provider_project_is_named_after_the_settings(self):
        self.pull()

        project = self.owner().project
        self.assertEqual(project.name, f"{self.settings.name} provider networks")
        self.assertTrue(
            structure_models.Project.available_objects.filter(pk=project.pk).exists()
        )


class WildcardShareTest(UnmanagedOwnerShareMixin, TestCase):
    """A provider network is usually shared with every project (`*`) at once."""

    def setUp(self):
        super().setUp()
        self.other_consumer = factories.TenantFactory(
            service_settings=self.settings, backend_id="other-consumer-id"
        )
        self.share([policy(target="*")])

    def test_wildcard_share_reaches_every_tenant(self):
        self.pull()
        self.pull(self.other_consumer)

        network = models.Network.objects.get(backend_id=NETWORK_ID)
        self.assertEqual(network.tenant, self.owner())
        self.assertEqual(
            set(
                models.NetworkRBACPolicy.objects.filter(
                    backend_id=POLICY_ID
                ).values_list("network", "target_tenant")
            ),
            {(network.id, self.consumer.id), (network.id, self.other_consumer.id)},
        )
        subnet = models.SubNet.objects.get(backend_id=SUBNET_ID)
        self.assertIn(subnet, self.consumer.available_subnets)
        self.assertIn(subnet, self.other_consumer.available_subnets)

    def test_repeated_pull_keeps_one_row_per_tenant(self):
        self.pull()
        self.pull(self.other_consumer)

        self.pull()

        self.assertEqual(
            models.NetworkRBACPolicy.objects.filter(backend_id=POLICY_ID).count(), 2
        )

    def test_named_share_takes_the_place_of_the_wildcard(self):
        self.pull()
        self.share(
            [
                policy(target="*"),
                policy("policy-2", target=self.consumer.backend_id),
            ]
        )

        self.pull()

        self.assertEqual(
            list(
                models.NetworkRBACPolicy.objects.filter(
                    target_tenant=self.consumer
                ).values_list("backend_id", flat=True)
            ),
            ["policy-2"],
        )

    def test_external_wildcard_is_not_imported(self):
        self.share([policy(target="*", action="access_as_external")])

        self.pull()

        self.assertFalse(models.Network.objects.filter(backend_id=NETWORK_ID).exists())
        self.neutron.show_network.assert_not_called()

    def test_wildcard_share_of_a_tenant_network_is_not_imported(self):
        """Revoking one of its rows from Waldur would unshare it for everyone."""
        network = factories.NetworkFactory(
            tenant=self.consumer,
            service_settings=self.settings,
            backend_id=NETWORK_ID,
        )

        self.pull()
        self.pull(self.other_consumer)

        self.assertFalse(network.rbac_policies.exists())

    def test_network_imported_meanwhile_is_not_copied(self):
        """Tenant pulls run at once; the later imports find the first one's."""
        self.pull()

        self.backend._import_shared_network_of_unmanaged_owner(
            self.other_consumer, policy(target="*")
        )

        network = models.Network.objects.get(backend_id=NETWORK_ID)
        self.assertEqual(models.Tenant.objects.filter(is_managed=False).count(), 1)
        self.assertEqual(
            set(network.rbac_policies.values_list("target_tenant", flat=True)),
            {self.consumer.id, self.other_consumer.id},
        )

    def test_withdrawn_wildcard_drops_the_network_locally(self):
        self.pull()
        self.pull(self.other_consumer)
        self.share([])

        self.pull()
        self.pull(self.other_consumer)

        self.assertFalse(models.Network.objects.filter(backend_id=NETWORK_ID).exists())
        self.assertFalse(models.Tenant.objects.filter(is_managed=False).exists())
        self.neutron.delete_rbac_policy.assert_not_called()


class SharesAtTenantCreationTest(UnmanagedOwnerShareMixin, TestCase):
    def test_creation_pulls_the_shares(self):
        chain = executors.get_tenant_create_tasks(self.consumer)

        steps = [task.args[1] for task in chain.tasks if len(task.args) > 1]

        self.assertIn("pull_tenant_network_rbac_policies_safely", steps)

    def test_failed_listing_does_not_fail_the_tenant(self):
        self.neutron.list_rbac_policies.side_effect = (
            neutron_exceptions.NeutronClientException()
        )

        self.backend.pull_tenant_network_rbac_policies_safely(self.consumer)

        self.assertFalse(models.Network.objects.filter(backend_id=NETWORK_ID).exists())

    def test_failed_import_does_not_fail_the_tenant(self):
        self.share([policy(target="*")])
        with mock.patch.object(
            OpenStackBackend, "pull_subnets", side_effect=RuntimeError("boom")
        ):
            self.backend.pull_tenant_network_rbac_policies_safely(self.consumer)

        self.assertFalse(models.Network.objects.filter(backend_id=NETWORK_ID).exists())


class CloudRefreshTest(UnmanagedOwnerShareMixin, TestCase):
    """The network is the cloud's, so it is refreshed once per cloud."""

    def setUp(self):
        super().setUp()
        self.other_consumer = factories.TenantFactory(
            service_settings=self.settings,
            backend_id="other-consumer-project-id",
            state=CoreStates.OK,
        )
        self.share([policy(target="*")])
        self.pull()
        self.pull(self.other_consumer)
        # Only the tenants live in the settings' domain; the admin project
        # that owns the network does not.
        self.keystone.projects.list.return_value = [
            mock.Mock(id=tenant.backend_id, description="")
            for tenant in (self.consumer, self.other_consumer)
        ]
        for tenant, project in zip(
            (self.consumer, self.other_consumer),
            self.keystone.projects.list.return_value,
        ):
            project.name = tenant.name
        self.neutron.reset_mock()

    def test_pulls_of_the_shares_leave_it_alone(self):
        self.pull()
        self.pull(self.other_consumer)

        self.neutron.show_network.assert_not_called()
        self.neutron.list_subnets.assert_not_called()
        self.neutron.list_ports.assert_not_called()

    def test_it_is_refreshed_on_a_schedule(self):
        schedule = OpenStackExtension.celery_tasks()

        self.assertIn(
            "openstack.pull_networks_of_unmanaged_owners",
            [entry["task"] for entry in schedule.values()],
        )

    def test_it_is_refreshed_once(self):
        # The settings are the scope of no offering, so the cloud's pull of its
        # resources would never run for them.
        self.backend_network["name"] = "provider-lan-renamed"
        self.neutron.list_subnets.return_value = {
            "subnets": [{**BACKEND_SUBNET, "name": "provider-lan-subnet-renamed"}]
        }

        tasks.pull_networks_of_unmanaged_owners()

        self.assertEqual(
            models.Network.objects.get(backend_id=NETWORK_ID).name,
            "provider-lan-renamed",
        )
        self.assertEqual(
            models.SubNet.objects.get(backend_id=SUBNET_ID).name,
            "provider-lan-subnet-renamed",
        )
        self.neutron.show_network.assert_called_once_with(NETWORK_ID)
        self.neutron.list_subnets.assert_called_once_with(network_id=NETWORK_ID)

    def test_cloud_pull_leaves_an_owner_outside_its_domain_ok(self):
        self.backend.pull_resources()

        owner = self.owner()
        self.assertEqual(owner.state, CoreStates.OK)
        self.assertEqual(owner.error_message, "")
        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.state, CoreStates.OK)

    def test_owner_marked_missing_before_recovers(self):
        owner = self.owner()
        handle_resource_not_found(owner)
        owner.refresh_from_db()
        self.assertEqual(owner.state, CoreStates.ERRED)

        tasks.pull_networks_of_unmanaged_owners()

        owner.refresh_from_db()
        self.assertEqual(owner.state, CoreStates.OK)
        self.assertEqual(owner.error_message, "")
        self.assertIsNone(owner.backend_missing_since)

    def test_a_failed_network_does_not_stop_the_others(self):
        other_network = {**BACKEND_NETWORK, "id": "provider-net-2", "name": "wan"}
        backend_networks = {
            NETWORK_ID: self.backend_network,
            "provider-net-2": other_network,
        }
        self.neutron.show_network.side_effect = lambda network_id: {
            "network": backend_networks[network_id]
        }
        self.share(
            [policy(target="*"), policy("policy-2", "*", object_id="provider-net-2")]
        )
        self.pull()

        # The failing network is the one imported first, so refreshed first.
        for error in (neutron_exceptions.NeutronClientException(), KeyError("id")):
            with self.subTest(error=type(error).__name__):
                other_network["name"] = f"wan-after-{type(error).__name__}"

                def show_network(network_id, error=error):
                    if network_id == NETWORK_ID:
                        raise error
                    return {"network": backend_networks[network_id]}

                self.neutron.show_network.side_effect = show_network

                tasks.pull_networks_of_unmanaged_owners()

                self.assertEqual(
                    models.Network.objects.get(backend_id="provider-net-2").name,
                    other_network["name"],
                )
                self.assertTrue(
                    models.Network.objects.filter(backend_id=NETWORK_ID).exists()
                )

    def test_shared_subnets_pull_leaves_it_to_the_cloud_pull(self):
        self.neutron.list_networks.return_value = {
            "networks": [{**BACKEND_NETWORK, "router:external": True, "shared": True}]
        }

        self.backend.pull_shared_subnets()

        self.neutron.list_subnets.assert_not_called()
        self.neutron.list_ports.assert_not_called()
