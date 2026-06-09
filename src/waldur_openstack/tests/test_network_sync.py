from unittest import mock

from rest_framework import test

from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.models import Network
from waldur_openstack.tests import fixtures


class NetworkSyncTest(test.APITestCase):
    def setUp(self):
        # Fixture for the network owner
        self.owner_fixture = fixtures.OpenStackFixture()
        self.owner_tenant = self.owner_fixture.tenant
        self.shared_network = self.owner_fixture.network
        self.shared_network.backend_id = "shared-network-id"
        self.shared_network.save()

        # Fixture for the network consumer
        self.consumer_fixture = fixtures.OpenStackFixture()
        self.consumer_tenant = self.consumer_fixture.tenant

        # Both tenants must belong to the same service settings for sharing to be valid
        self.consumer_tenant.service_settings = self.owner_fixture.settings
        self.consumer_tenant.save()

        self.backend = OpenStackBackend(self.owner_fixture.settings)

    @mock.patch("waldur_openstack.backend.OpenStackBackend.list_networks")
    def test_pulling_consumer_tenant_does_not_delete_shared_network(
        self, mock_list_networks
    ):
        """
        Verify that syncing the consumer tenant does not delete the shared network
        owned by another tenant.
        """
        # Arrange: Mock backend response for consumer tenant.
        mock_list_networks.return_value = [
            {
                "id": self.shared_network.backend_id,
                "name": self.shared_network.name,
                "tenant_id": self.owner_tenant.backend_id,  # Owner's ID
                "description": "",
                "router:external": False,
                "status": "ACTIVE",
            }
        ]

        # Act: Pull networks for the consumer tenant
        self.backend.pull_tenant_networks(self.consumer_tenant)

        # Assert: The shared network should still exist
        self.assertTrue(Network.objects.filter(pk=self.shared_network.pk).exists())
        mock_list_networks.assert_called_once_with(self.consumer_tenant.backend_id)

    @mock.patch("waldur_openstack.backend.OpenStackBackend.list_networks")
    def test_pulling_consumer_tenant_does_not_delete_wrongly_associated_shared_network(
        self, mock_list_networks
    ):
        """
        Verify that the fix prevents deletion even if the shared network is
        incorrectly associated with the consumer tenant in the database.
        """
        # Arrange: Corrupt the database by associating the shared network with the consumer
        self.shared_network.tenant = self.consumer_tenant
        self.shared_network.save()

        # Mock backend response for consumer tenant, with owner's tenant_id
        mock_list_networks.return_value = [
            {
                "id": self.shared_network.backend_id,
                "name": self.shared_network.name,
                "tenant_id": self.owner_tenant.backend_id,
                "description": "",
                "router:external": False,
                "status": "ACTIVE",
            }
        ]

        # Act: Pull networks for the consumer tenant
        self.backend.pull_tenant_networks(self.consumer_tenant)

        # Assert: The network should NOT be deleted
        self.assertTrue(Network.objects.filter(pk=self.shared_network.pk).exists())

    @mock.patch("waldur_openstack.backend.OpenStackBackend.list_networks")
    def test_pulling_consumer_creates_new_shared_network_with_correct_owner(
        self, mock_list_networks
    ):
        """
        When a shared network is not yet in the Waldur DB, syncing the consumer
        tenant should correctly create it and assign it to its actual owner.
        """
        # Arrange: Ensure the shared network does not exist in the database
        self.shared_network.delete()
        self.assertEqual(Network.objects.count(), 0)

        # Mock backend response for consumer tenant
        mock_list_networks.return_value = [
            {
                "id": "new-shared-network-id",
                "name": "New Shared Network",
                "tenant_id": self.owner_tenant.backend_id,  # Belongs to owner
                "description": "",
                "router:external": False,
                "status": "ACTIVE",
            }
        ]

        # Act: Pull networks for the consumer tenant
        self.backend.pull_tenant_networks(self.consumer_tenant)

        # Assert: A new network should have been created, and it MUST have the correct owner.
        self.assertEqual(Network.objects.count(), 1)
        new_network = Network.objects.first()
        self.assertEqual(new_network.backend_id, "new-shared-network-id")
        self.assertEqual(
            new_network.tenant,
            self.owner_tenant,
            "FIX FAILED: The newly created shared network has the wrong owner.",
        )

    @mock.patch("waldur_openstack.backend.OpenStackBackend.list_networks")
    def test_pulling_owner_tenant_corrects_wrongly_associated_network(
        self, mock_list_networks
    ):
        """
        Verify that syncing the owner tenant fixes the incorrect tenant association.
        """
        # Arrange: Corrupt the database
        self.shared_network.tenant = self.consumer_tenant
        self.shared_network.save()

        # Mock backend response for the actual OWNER tenant
        mock_list_networks.return_value = [
            {
                "id": self.shared_network.backend_id,
                "name": "Updated Name",
                "tenant_id": self.owner_tenant.backend_id,
                "description": "",
                "router:external": False,
                "status": "ACTIVE",
            }
        ]

        # Act: Pull networks for the correct owner tenant
        self.backend.pull_tenant_networks(self.owner_tenant)

        # Assert: The network's tenant association should be corrected back to the owner
        self.shared_network.refresh_from_db()
        self.assertEqual(
            self.shared_network.tenant,
            self.owner_tenant,
            "FIX FAILED: Tenant association was not corrected.",
        )
        self.assertEqual(self.shared_network.name, "Updated Name")

    @mock.patch("waldur_openstack.backend.OpenStackBackend.list_networks")
    def test_pulling_persists_port_security_enabled_false_from_backend(
        self, mock_list_networks
    ):
        # Arrange
        mock_list_networks.return_value = [
            {
                "id": self.shared_network.backend_id,
                "name": self.shared_network.name,
                "tenant_id": self.owner_tenant.backend_id,
                "description": "",
                "router:external": False,
                "status": "ACTIVE",
                "port_security_enabled": False,
            }
        ]

        # Act
        self.backend.pull_tenant_networks(self.owner_tenant)

        # Assert
        self.shared_network.refresh_from_db()
        self.assertFalse(self.shared_network.port_security_enabled)

    @mock.patch("waldur_openstack.backend.OpenStackBackend.list_networks")
    def test_pulling_defaults_port_security_enabled_to_true_when_absent(
        self, mock_list_networks
    ):
        # Arrange: backend payload omits port_security_enabled (older Neutron).
        mock_list_networks.return_value = [
            {
                "id": "fresh-network-id",
                "name": "Fresh Network",
                "tenant_id": self.owner_tenant.backend_id,
                "description": "",
                "router:external": False,
                "status": "ACTIVE",
            }
        ]

        # Act
        self.backend.pull_tenant_networks(self.owner_tenant)

        # Assert: newly imported network defaults to True.
        new_network = Network.objects.get(backend_id="fresh-network-id")
        self.assertTrue(new_network.port_security_enabled)
