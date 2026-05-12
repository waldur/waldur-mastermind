from unittest import mock

from django.test import TestCase

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.tests.fixtures import mock_session

from . import factories, fixtures


class NetworkRBACPolicySyncTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.backend = OpenStackBackend(self.tenant.service_settings)

        # Mock neutron client
        self.neutron_patcher = mock.patch("waldur_openstack.backend.get_neutron_client")
        self.mock_neutron = self.neutron_patcher.start()

        mock_session()

    def tearDown(self):
        mock.patch.stopall()

    def test_sync_outgoing_policies_creates_new_policies(self):
        """Test that outgoing RBAC policies are created correctly."""
        # Create networks and target tenant
        network1 = factories.NetworkFactory(tenant=self.tenant, backend_id="net1")
        network2 = factories.NetworkFactory(tenant=self.tenant, backend_id="net2")
        target_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="target_tenant"
        )

        # Mock backend policies from OpenStack
        backend_policies = [
            {
                "id": "policy1",
                "object_id": "net1",
                "target_tenant": "target_tenant",
                "action": "access_as_shared",
            },
            {
                "id": "policy2",
                "object_id": "net2",
                "target_tenant": "target_tenant",
                "action": "access_as_external",
            },
        ]

        self.mock_neutron.return_value.list_rbac_policies.return_value = {
            "rbac_policies": backend_policies
        }

        # Execute sync
        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        # Verify policies were created
        policies = models.NetworkRBACPolicy.objects.all()
        self.assertEqual(policies.count(), 2)

        policy1 = policies.get(backend_id="policy1")
        self.assertEqual(policy1.network, network1)
        self.assertEqual(policy1.target_tenant, target_tenant)
        self.assertEqual(policy1.policy_type, "access_as_shared")

        policy2 = policies.get(backend_id="policy2")
        self.assertEqual(policy2.network, network2)
        self.assertEqual(policy2.target_tenant, target_tenant)
        self.assertEqual(policy2.policy_type, "access_as_external")

    def test_sync_incoming_policies_creates_new_policies(self):
        """Test that incoming RBAC policies are created correctly."""
        # Create a network owned by another tenant
        source_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="source_tenant"
        )
        shared_network = factories.NetworkFactory(
            tenant=source_tenant, backend_id="shared_net"
        )

        # Mock backend policies - network shared TO our tenant
        backend_policies = [
            {
                "id": "incoming_policy1",
                "object_id": "shared_net",
                "target_tenant": self.tenant.backend_id,
                "action": "access_as_shared",
            },
        ]

        self.mock_neutron.return_value.list_rbac_policies.return_value = {
            "rbac_policies": backend_policies
        }

        # Execute sync
        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        # Verify incoming policy was created
        policy = models.NetworkRBACPolicy.objects.get(backend_id="incoming_policy1")
        self.assertEqual(policy.network, shared_network)
        self.assertEqual(policy.target_tenant, self.tenant)
        self.assertEqual(policy.policy_type, "access_as_shared")

    def test_sync_incoming_policies_works_without_tenant_networks(self):
        """Test that incoming RBAC policies are synced even when tenant has no networks."""
        # Ensure tenant has no networks
        self.tenant.networks.all().delete()

        # Create a network owned by another tenant
        source_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="source_tenant"
        )
        shared_network = factories.NetworkFactory(
            tenant=source_tenant, backend_id="shared_net"
        )

        # Mock backend policies - network shared TO our tenant
        backend_policies = [
            {
                "id": "incoming_policy_no_nets",
                "object_id": "shared_net",
                "target_tenant": self.tenant.backend_id,
                "action": "access_as_external",
            },
        ]

        self.mock_neutron.return_value.list_rbac_policies.return_value = {
            "rbac_policies": backend_policies
        }

        # Execute sync - should NOT exit early anymore
        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        # Verify incoming policy was created even without tenant networks
        policy = models.NetworkRBACPolicy.objects.get(
            backend_id="incoming_policy_no_nets"
        )
        self.assertEqual(policy.network, shared_network)
        self.assertEqual(policy.target_tenant, self.tenant)
        self.assertEqual(policy.policy_type, "access_as_external")

    def test_sync_removes_stale_policy_before_inserting_recreated_one(self):
        """When OpenStack recreates an RBAC policy the new entry has a fresh
        ``backend_id`` but reuses the same
        ``(network, target_tenant, policy_type)`` trio that the database has a
        unique constraint on.

        The sync treats the new ``backend_id`` as a new object: it deletes the
        stale Waldur row and inserts a fresh one — it does NOT rename the
        existing row's ``backend_id``. The new row therefore has a different
        primary key and UUID from the old one, matching how Waldur handles
        every other recreated OpenStack subresource.

        Without the upfront delete, the loop would attempt INSERT before the
        stale-cleanup runs and crash with IntegrityError on every poll.
        """
        source_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="source_tenant"
        )
        shared_network = factories.NetworkFactory(
            tenant=source_tenant, backend_id="shared_net"
        )

        old_policy = factories.NetworkRBACPolicyFactory(
            network=shared_network,
            target_tenant=self.tenant,
            backend_id="old_backend_id",
            policy_type="access_as_shared",
        )
        old_pk = old_policy.pk
        old_uuid = old_policy.uuid

        backend_policies = [
            {
                "id": "new_backend_id",
                "object_id": "shared_net",
                "target_tenant": self.tenant.backend_id,
                "action": "access_as_shared",
            },
        ]

        self.mock_neutron.return_value.list_rbac_policies.return_value = {
            "rbac_policies": backend_policies
        }

        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        # Old Waldur row is gone — not "renamed" by overwriting backend_id.
        self.assertFalse(
            models.NetworkRBACPolicy.objects.filter(pk=old_pk).exists(),
            "Recreate semantics require the old row to be deleted, not updated.",
        )

        # Exactly one row exists for the trio, and it is a fresh object with
        # a different PK and UUID from the one we started with.
        policies = models.NetworkRBACPolicy.objects.filter(
            network=shared_network,
            target_tenant=self.tenant,
            policy_type="access_as_shared",
        )
        self.assertEqual(policies.count(), 1)
        new_policy = policies.first()
        self.assertEqual(new_policy.backend_id, "new_backend_id")
        self.assertNotEqual(
            new_policy.pk, old_pk, "New row must have a different primary key."
        )
        self.assertNotEqual(
            new_policy.uuid,
            old_uuid,
            "New row must have a different UUID (it is a new Waldur object).",
        )

    def test_sync_updates_existing_policies(self):
        """Existing policy whose backend_id stays the same is updated in place."""
        network = factories.NetworkFactory(tenant=self.tenant, backend_id="net1")
        target_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="target_tenant"
        )

        existing_policy = factories.NetworkRBACPolicyFactory(
            network=network,
            target_tenant=target_tenant,
            backend_id="policy1",
            policy_type="access_as_shared",
        )

        backend_policies = [
            {
                "id": "policy1",
                "object_id": "net1",
                "target_tenant": "target_tenant",
                "action": "access_as_external",
            },
        ]

        self.mock_neutron.return_value.list_rbac_policies.return_value = {
            "rbac_policies": backend_policies
        }

        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        existing_policy.refresh_from_db()
        self.assertEqual(existing_policy.policy_type, "access_as_external")

    def test_sync_deletes_stale_outgoing_policies(self):
        """Test that stale outgoing policies are deleted."""
        network = factories.NetworkFactory(tenant=self.tenant, backend_id="net1")
        target_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="target_tenant"
        )

        # Create policy that no longer exists in backend
        stale_policy = factories.NetworkRBACPolicyFactory(
            network=network, target_tenant=target_tenant, backend_id="stale_policy"
        )

        # Mock empty backend policies
        self.mock_neutron.return_value.list_rbac_policies.return_value = {
            "rbac_policies": []
        }

        # Execute sync
        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        # Verify stale policy was deleted
        self.assertFalse(
            models.NetworkRBACPolicy.objects.filter(id=stale_policy.id).exists()
        )

    def test_sync_deletes_stale_incoming_policies(self):
        """Test that stale incoming policies are deleted."""
        source_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="source_tenant"
        )
        shared_network = factories.NetworkFactory(
            tenant=source_tenant, backend_id="shared_net"
        )

        # Create incoming policy that no longer exists in backend
        stale_incoming_policy = factories.NetworkRBACPolicyFactory(
            network=shared_network,
            target_tenant=self.tenant,
            backend_id="stale_incoming_policy",
        )

        # Mock empty backend policies
        self.mock_neutron.return_value.list_rbac_policies.return_value = {
            "rbac_policies": []
        }

        # Execute sync
        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        # Verify stale incoming policy was deleted
        self.assertFalse(
            models.NetworkRBACPolicy.objects.filter(
                id=stale_incoming_policy.id
            ).exists()
        )

    def test_sync_skips_networks_from_different_service(self):
        """Test that policies for networks from different services are skipped."""
        # Create another service settings
        other_service = factories.SettingsFactory()
        other_tenant = factories.TenantFactory(
            service_settings=other_service, backend_id="other_service_tenant"
        )
        factories.NetworkFactory(tenant=other_tenant, backend_id="other_net")

        # Mock backend policies that reference the other service network
        backend_policies = [
            {
                "id": "cross_service_policy",
                "object_id": "other_net",
                "target_tenant": self.tenant.backend_id,
                "action": "access_as_shared",
            },
        ]

        self.mock_neutron.return_value.list_rbac_policies.return_value = {
            "rbac_policies": backend_policies
        }

        # Execute sync
        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        # Verify no policy was created (should be skipped)
        self.assertFalse(
            models.NetworkRBACPolicy.objects.filter(
                backend_id="cross_service_policy"
            ).exists()
        )

        self.assertTrue(
            Event.objects.filter(event_type=EventType.OPENSTACK_NETWORK_PULLED).exists()
        )

    def test_sync_handles_nonexistent_networks(self):
        """Test that policies for non-existent networks are handled gracefully."""
        factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="target_tenant"
        )

        # Mock backend policies that reference non-existent network
        backend_policies = [
            {
                "id": "orphan_policy",
                "object_id": "nonexistent_net",
                "target_tenant": "target_tenant",
                "action": "access_as_shared",
            },
        ]

        self.mock_neutron.return_value.list_rbac_policies.return_value = {
            "rbac_policies": backend_policies
        }

        # Execute sync - should not raise exception
        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        # Verify no policy was created for non-existent network
        self.assertFalse(
            models.NetworkRBACPolicy.objects.filter(backend_id="orphan_policy").exists()
        )

    def test_sync_handles_nonexistent_target_tenant(self):
        """Test that policies for non-existent target tenants are handled gracefully."""
        factories.NetworkFactory(tenant=self.tenant, backend_id="net1")

        # Mock backend policies that reference non-existent target tenant
        backend_policies = [
            {
                "id": "orphan_target_policy",
                "object_id": "net1",
                "target_tenant": "nonexistent_tenant",
                "action": "access_as_shared",
            },
        ]

        self.mock_neutron.return_value.list_rbac_policies.return_value = {
            "rbac_policies": backend_policies
        }

        # Execute sync - should not raise exception
        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        # Verify no policy was created for non-existent target tenant
        self.assertFalse(
            models.NetworkRBACPolicy.objects.filter(
                backend_id="orphan_target_policy"
            ).exists()
        )

    def test_sync_processes_policies_even_without_networks(self):
        """Test that sync still processes incoming policies even if tenant has no networks."""
        # Ensure tenant has no networks
        self.tenant.networks.all().delete()

        # Execute sync
        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        # Verify neutron client was still called (for incoming policies)
        self.mock_neutron.return_value.list_rbac_policies.assert_called_once()

    def test_sync_handles_neutron_exceptions(self):
        """Test that neutron client exceptions are handled properly."""
        factories.NetworkFactory(tenant=self.tenant, backend_id="net1")

        # Mock neutron client to raise exception
        from neutronclient.common import exceptions as neutron_exceptions

        self.mock_neutron.return_value.list_rbac_policies.side_effect = (
            neutron_exceptions.NeutronClientException("Connection error")
        )

        # Execute sync - should raise OpenStackBackendError
        from waldur_openstack.backend import OpenStackBackendError

        with self.assertRaises(OpenStackBackendError):
            self.backend.pull_tenant_network_rbac_policies(self.tenant)

    def test_sync_complete_bidirectional_scenario(self):
        """Test a complete scenario with both outgoing and incoming policies."""
        # Create networks and tenants
        own_network = factories.NetworkFactory(tenant=self.tenant, backend_id="own_net")

        source_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="source_tenant"
        )
        shared_network = factories.NetworkFactory(
            tenant=source_tenant, backend_id="shared_net"
        )

        target_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="target_tenant"
        )

        # Mock both outgoing and incoming policies
        backend_policies = [
            # Outgoing: our network shared to target tenant
            {
                "id": "outgoing_policy",
                "object_id": "own_net",
                "target_tenant": "target_tenant",
                "action": "access_as_shared",
            },
            # Incoming: source tenant's network shared to us
            {
                "id": "incoming_policy",
                "object_id": "shared_net",
                "target_tenant": self.tenant.backend_id,
                "action": "access_as_external",
            },
        ]

        self.mock_neutron.return_value.list_rbac_policies.return_value = {
            "rbac_policies": backend_policies
        }

        # Execute sync
        self.backend.pull_tenant_network_rbac_policies(self.tenant)

        # Verify both policies were created
        policies = models.NetworkRBACPolicy.objects.all()
        self.assertEqual(policies.count(), 2)

        outgoing_policy = policies.get(backend_id="outgoing_policy")
        self.assertEqual(outgoing_policy.network, own_network)
        self.assertEqual(outgoing_policy.target_tenant, target_tenant)

        incoming_policy = policies.get(backend_id="incoming_policy")
        self.assertEqual(incoming_policy.network, shared_network)
        self.assertEqual(incoming_policy.target_tenant, self.tenant)
