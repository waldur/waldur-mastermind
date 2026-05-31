"""Test auto-recovery of external network configuration during tenant sync."""

from unittest import mock

from neutronclient.common import exceptions as neutron_exceptions
from rest_framework import test

from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.tests import factories, fixtures


class ExternalNetworkAutoRecoveryTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.backend = OpenStackBackend(self.tenant.service_settings)

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_detect_external_network_finds_existing_router_with_gateway(
        self, mock_neutron_client, mock_session
    ):
        """Test that detect_external_network finds and sets external network from existing router."""
        # Setup mock
        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client

        # Mock router with external gateway
        mock_client.list_routers.return_value = {
            "routers": [
                {
                    "id": "router-123",
                    "external_gateway_info": {"network_id": "ext-net-456"},
                }
            ]
        }

        # Clear existing external network ID
        self.tenant.external_network_id = ""
        self.tenant.save()

        # Execute
        self.backend.detect_external_network(self.tenant)

        # Verify
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.external_network_id, "ext-net-456")

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_detect_external_network_attempts_auto_recovery_when_no_router_exists(
        self, mock_neutron_client, mock_session
    ):
        """Test auto-recovery when tenant has no router but external network is configured."""
        # Setup mock
        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client

        # Mock no routers exist
        mock_client.list_routers.return_value = {"routers": []}

        # Setup external network in service settings
        self.tenant.service_settings.options = {"external_network_id": "ext-net-789"}
        self.tenant.service_settings.save()

        # Clear existing external network ID
        self.tenant.external_network_id = ""
        self.tenant.save()

        # Mock connect_tenant_to_external_network
        with mock.patch.object(
            self.backend, "connect_tenant_to_external_network"
        ) as mock_connect:
            # Execute
            self.backend.detect_external_network(self.tenant)

            # Verify auto-recovery was attempted
            mock_connect.assert_called_once_with(self.tenant, "ext-net-789")

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_detect_external_network_attempts_recovery_when_router_has_no_gateway(
        self, mock_neutron_client, mock_session
    ):
        """Test auto-recovery when tenant has router but no external gateway."""
        # Setup mock
        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client

        # Mock router without external gateway
        mock_client.list_routers.return_value = {
            "routers": [
                {
                    "id": "router-123",
                    "external_gateway_info": {},  # No network_id
                }
            ]
        }

        # Setup external network in service settings
        self.tenant.service_settings.options = {"external_network_id": "ext-net-999"}
        self.tenant.service_settings.save()

        # Clear existing external network ID
        self.tenant.external_network_id = ""
        self.tenant.save()

        # Mock connect_tenant_to_external_network
        with mock.patch.object(
            self.backend, "connect_tenant_to_external_network"
        ) as mock_connect:
            # Execute
            self.backend.detect_external_network(self.tenant)

            # Verify auto-recovery was attempted
            mock_connect.assert_called_once_with(self.tenant, "ext-net-999")

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_detect_external_network_logs_warning_on_recovery_failure(
        self, mock_neutron_client, mock_session
    ):
        """Test that recovery failure is logged but doesn't raise exception."""
        # Setup mock
        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client

        # Mock no routers
        mock_client.list_routers.return_value = {"routers": []}

        # Setup external network in service settings
        self.tenant.service_settings.options = {"external_network_id": "ext-net-fail"}
        self.tenant.service_settings.save()

        # Clear existing external network ID
        self.tenant.external_network_id = ""
        self.tenant.save()

        # Mock connect_tenant_to_external_network to fail
        with mock.patch.object(
            self.backend,
            "connect_tenant_to_external_network",
            side_effect=Exception("Connection failed"),
        ):
            with mock.patch("waldur_openstack.backend.logger") as mock_logger:
                # Execute - should not raise exception
                self.backend.detect_external_network(self.tenant)

                # Verify warning was logged
                mock_logger.warning.assert_called()
                warning_call = mock_logger.warning.call_args[0][0]
                self.assertIn("Auto-recovery failed", warning_call)
                self.assertIn("Manual intervention may be required", warning_call)

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_detect_external_network_skips_recovery_if_already_set(
        self, mock_neutron_client, mock_session
    ):
        """Test that auto-recovery is skipped if external_network_id is already set."""
        # Setup mock
        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client

        # Mock router without external gateway
        mock_client.list_routers.return_value = {
            "routers": [{"id": "router-123", "external_gateway_info": {}}]
        }

        # External network already set on tenant
        self.tenant.external_network_id = "existing-ext-net"
        self.tenant.save()

        # Mock connect_tenant_to_external_network
        with mock.patch.object(
            self.backend, "connect_tenant_to_external_network"
        ) as mock_connect:
            # Execute
            self.backend.detect_external_network(self.tenant)

            # Verify auto-recovery was NOT attempted
            mock_connect.assert_not_called()

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_detect_external_network_selects_specified_router_when_provided(
        self, mock_neutron_client, mock_session
    ):
        factories.RouterFactory(tenant=self.tenant, backend_id="router-1")
        router2 = factories.RouterFactory(tenant=self.tenant, backend_id="router-2")

        mock_neutron_client().list_routers.return_value = {
            "routers": [
                {
                    "id": "router-1",
                    "external_gateway_info": {"network_id": "ext-net-1"},
                },
                {
                    "id": "router-2",
                    "external_gateway_info": {"network_id": "ext-net-2"},
                },
            ]
        }

        self.tenant.external_network_id = ""
        self.tenant.save()

        self.backend.detect_external_network(self.tenant, router=router2)

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.external_network_id, "ext-net-2")


class FloatingIPCreationRecoveryTest(test.APITestCase):
    """Test auto-recovery during floating IP creation."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.backend = OpenStackBackend(self.tenant.service_settings)
        # Create a floating IP object using factory with proper tenant
        from waldur_openstack.tests import factories

        self.floating_ip = factories.FloatingIPFactory(tenant=self.tenant)

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_create_floating_ip_attempts_recovery_when_tenant_missing_external_network(
        self, mock_neutron_client, mock_session
    ):
        """Test that floating IP creation attempts recovery when tenant lacks external network."""
        # Setup mock
        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client

        # Setup external network in service settings
        self.tenant.service_settings.options = {"external_network_id": "ext-net-456"}
        self.tenant.service_settings.save()

        # Clear tenant's external network ID
        self.tenant.external_network_id = ""
        self.tenant.save()

        # Mock successful floating IP creation
        mock_client.create_floatingip.return_value = {
            "floatingip": {
                "id": "fip-123",
                "floating_ip_address": "10.0.0.1",
                "floating_network_id": "ext-net-456",
                "status": "ACTIVE",
            }
        }

        # Mock detect_external_network to simulate recovery
        with mock.patch.object(self.backend, "detect_external_network") as mock_detect:
            # Execute
            self.backend.create_floating_ip(self.floating_ip)

            # Verify recovery was attempted
            mock_detect.assert_called_once_with(self.tenant, router=None)

            # Verify floating IP was created with external network from settings
            mock_client.create_floatingip.assert_called_once()
            call_args = mock_client.create_floatingip.call_args[0][0]
            self.assertEqual(
                call_args["floatingip"]["floating_network_id"], "ext-net-456"
            )

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_create_floating_ip_proceeds_with_settings_value_if_recovery_fails(
        self, mock_neutron_client, mock_session
    ):
        """Test that floating IP creation uses settings value if recovery fails."""
        # Setup mock
        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client

        # Setup external network in service settings
        self.tenant.service_settings.options = {"external_network_id": "ext-net-789"}
        self.tenant.service_settings.save()

        # Clear tenant's external network ID
        self.tenant.external_network_id = ""
        self.tenant.save()

        # Mock successful floating IP creation
        mock_client.create_floatingip.return_value = {
            "floatingip": {
                "id": "fip-456",
                "floating_ip_address": "10.0.0.2",
                "floating_network_id": "ext-net-789",
                "status": "ACTIVE",
            }
        }

        # Mock detect_external_network to fail
        with mock.patch.object(
            self.backend,
            "detect_external_network",
            side_effect=Exception("Recovery failed"),
        ):
            with mock.patch("waldur_openstack.backend.logger") as mock_logger:
                # Execute - should not raise exception
                self.backend.create_floating_ip(self.floating_ip)

                # Verify warning was logged
                mock_logger.warning.assert_called()
                warning_call = mock_logger.warning.call_args[0][0]
                self.assertIn("Failed to recover external network", warning_call)

                # Verify floating IP was created with settings value
                mock_client.create_floatingip.assert_called_once()
                call_args = mock_client.create_floatingip.call_args[0][0]
                self.assertEqual(
                    call_args["floatingip"]["floating_network_id"], "ext-net-789"
                )

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_create_floating_ip_skips_recovery_if_tenant_has_external_network(
        self, mock_neutron_client, mock_session
    ):
        """Test that recovery is skipped if tenant already has external network."""
        # Setup mock
        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client

        # Tenant already has external network
        self.tenant.external_network_id = "existing-ext-net"
        self.tenant.save()

        # Service settings can have a different value
        self.tenant.service_settings.options = {
            "external_network_id": "settings-ext-net"
        }
        self.tenant.service_settings.save()

        # Mock successful floating IP creation
        mock_client.create_floatingip.return_value = {
            "floatingip": {
                "id": "fip-789",
                "floating_ip_address": "10.0.0.3",
                "floating_network_id": "existing-ext-net",
                "status": "ACTIVE",
            }
        }

        # Mock detect_external_network
        with mock.patch.object(self.backend, "detect_external_network") as mock_detect:
            # Execute
            self.backend.create_floating_ip(self.floating_ip)

            # Verify recovery was NOT attempted
            mock_detect.assert_not_called()

            # Verify floating IP was created with tenant's external network
            mock_client.create_floatingip.assert_called_once()
            call_args = mock_client.create_floatingip.call_args[0][0]
            self.assertEqual(
                call_args["floatingip"]["floating_network_id"], "existing-ext-net"
            )


class FloatingIPPoolExhaustionMessageTest(test.APITestCase):
    """When Neutron rejects FIP allocation because the external network's
    pool is exhausted, the user-facing error must name the pool so the
    caller knows what to do."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.tenant.external_network_id = "ext-net-cached"
        self.tenant.save(update_fields=["external_network_id"])
        # Pre-create the ExternalNetwork cache row so the helper finds a name.
        factories.ExternalNetworkFactory(
            settings=self.tenant.service_settings,
            backend_id="ext-net-cached",
            name="public-pool",
        )
        self.backend = OpenStackBackend(self.tenant.service_settings)
        self.floating_ip = factories.FloatingIPFactory(tenant=self.tenant)

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_ip_address_generation_failure_raises_named_pool_error(
        self, mock_neutron_client, _mock_session
    ):
        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client
        mock_client.create_floatingip.side_effect = (
            neutron_exceptions.IpAddressGenerationFailureClient(
                "No more IP addresses available on network"
            )
        )

        with self.assertRaises(OpenStackBackendError) as ctx:
            self.backend.create_floating_ip(self.floating_ip)

        message = str(ctx.exception)
        # The named pool must appear in the message so the caller can act.
        self.assertIn("public-pool", message)
        self.assertIn("cloud administrator", message)
        # The FIP row must be marked ERRED.
        self.floating_ip.refresh_from_db()
        self.assertEqual(self.floating_ip.runtime_state, "ERRED")

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_external_ip_exhausted_raises_named_pool_error(
        self, mock_neutron_client, _mock_session
    ):
        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client
        mock_client.create_floatingip.side_effect = (
            neutron_exceptions.ExternalIpAddressExhaustedClient(
                "All external IPs allocated"
            )
        )

        with self.assertRaises(OpenStackBackendError) as ctx:
            self.backend.create_floating_ip(self.floating_ip)

        self.assertIn("public-pool", str(ctx.exception))

    @mock.patch("waldur_openstack.backend.get_tenant_session")
    @mock.patch("waldur_openstack.backend.get_neutron_client")
    def test_pool_error_falls_back_to_backend_id_if_name_not_cached(
        self, mock_neutron_client, _mock_session
    ):
        # Remove the cached ExternalNetwork row.
        from waldur_openstack import models

        models.ExternalNetwork.objects.filter(
            settings=self.tenant.service_settings, backend_id="ext-net-cached"
        ).delete()

        mock_client = mock.MagicMock()
        mock_neutron_client.return_value = mock_client
        mock_client.create_floatingip.side_effect = (
            neutron_exceptions.IpAddressGenerationFailureClient("pool exhausted")
        )

        with self.assertRaises(OpenStackBackendError) as ctx:
            self.backend.create_floating_ip(self.floating_ip)

        # Backend ID must appear when the human name isn't cached.
        self.assertIn("ext-net-cached", str(ctx.exception))
