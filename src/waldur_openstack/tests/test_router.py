from unittest import mock

from ddt import data, ddt
from django.test import override_settings
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.permissions.fixtures import ProjectRole
from waldur_openstack import models

from . import factories, fixtures


class BaseRouterTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.owner)
        self.mock = mock.patch("waldur_openstack.backend.OpenStackBackend.get_free_ip")
        self.mock_get_free_ip = self.mock.start()
        self.mock_get_free_ip.return_value = "1.1.1.1"
        self.mock = mock.patch("waldur_openstack.backend.OpenStackBackend.create_port")
        self.mock_create_port = self.mock.start()


class SetRoutesTest(BaseRouterTest):
    def setUp(self):
        super().setUp()
        self.router = factories.RouterFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.url = factories.RouterFactory.get_url(self.router, action="set_routes")

    def test_positive(self):
        response = self.client.post(
            self.url, {"routes": [{"destination": "1.1.1.1", "nexthop": "10.10.10.10"}]}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_negative(self):
        response = self.client.post(
            self.url,
            {"routes": [{"destination": "1.1.1.1/33", "nexthop": "10.10.10.10"}]},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RouterInterfaceTest(BaseRouterTest):
    def setUp(self):
        super().setUp()
        self.router = factories.RouterFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.subnet = factories.SubNetFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.port = factories.PortFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.url_add = factories.RouterFactory.get_url(
            self.router, action="add_router_interface"
        )
        self.url_remove = factories.RouterFactory.get_url(
            self.router, action="remove_router_interface"
        )

    @mock.patch("waldur_openstack.backend.OpenStackBackend.pull_tenant_routers")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.add_router_interface")
    @mock.patch(
        "waldur_openstack.backend.OpenStackBackend.get_free_ip",
        return_value="192.168.1.10",
    )
    def test_add_router_interface_with_subnet(
        self, mock_get_free_ip, mock_add, mock_pull
    ):
        response = self.client.post(
            self.url_add, {"subnet": factories.SubNetFactory.get_url(self.subnet)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        mock_add.assert_called_once()
        mock_pull.assert_called_once()

    @mock.patch("waldur_openstack.backend.OpenStackBackend.pull_tenant_routers")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.add_router_interface")
    def test_add_router_interface_with_port(self, mock_add, mock_pull):
        response = self.client.post(
            self.url_add, {"port": factories.PortFactory.get_url(self.port)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_add.assert_called_once()
        mock_pull.assert_called_once()

    def test_add_router_interface_missing_params(self):
        response = self.client.post(self.url_add, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_add_router_interface_both_params(self):
        response = self.client.post(
            self.url_add,
            {
                "subnet": factories.SubNetFactory.get_url(self.subnet),
                "port": factories.PortFactory.get_url(self.port),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_add_router_interface_wrong_tenant(self):
        other_tenant = factories.TenantFactory()
        other_subnet = factories.SubNetFactory(tenant=other_tenant)
        response = self.client.post(
            self.url_add, {"subnet": factories.SubNetFactory.get_url(other_subnet)}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(task_always_eager=True)
    @mock.patch("waldur_openstack.backend.OpenStackBackend.pull_tenant_routers")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.remove_router_interface")
    def test_remove_router_interface_with_subnet(self, mock_remove, mock_pull):
        response = self.client.post(
            self.url_remove, {"subnet": factories.SubNetFactory.get_url(self.subnet)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_remove.assert_called_once()
        mock_pull.assert_called_once()

    @override_settings(task_always_eager=True)
    @mock.patch("waldur_openstack.backend.OpenStackBackend.pull_tenant_routers")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.remove_router_interface")
    def test_remove_router_interface_with_port(self, mock_remove, mock_pull):
        response = self.client.post(
            self.url_remove, {"port": factories.PortFactory.get_url(self.port)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_remove.assert_called_once()
        mock_pull.assert_called_once()

    def test_remove_router_interface_missing_params(self):
        response = self.client.post(self.url_remove, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_remove_router_interface_both_params(self):
        response = self.client.post(
            self.url_remove,
            {
                "subnet": factories.SubNetFactory.get_url(self.subnet),
                "port": factories.PortFactory.get_url(self.port),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_remove_router_interface_wrong_tenant(self):
        other_tenant = factories.TenantFactory()
        other_port = factories.PortFactory(tenant=other_tenant)
        response = self.client.post(
            self.url_remove, {"port": factories.PortFactory.get_url(other_port)}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
@mock.patch("waldur_openstack.executors.RouterCreateExecutor.execute")
class RouterCreateTest(BaseRouterTest):
    def setUp(self):
        super().setUp()
        self.url = factories.RouterFactory.get_list_url()

        self.valid_data = {
            "name": "Test Router",
            "tenant": factories.TenantFactory.get_url(self.fixture.tenant),
        }

    @data("admin", "manager", "staff", "owner")
    def test_user_can_create_router(self, user, create_port_executor_mock):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        create_port_executor_mock.assert_called_once()

    @data("user", "member", "global_support")
    def test_user_can_not_create_router(self, user, create_port_executor_mock):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        create_port_executor_mock.assert_not_called()

    def test_router_cannot_be_created_without_name(self, create_port_executor_mock):
        self.client.force_authenticate(self.fixture.owner)
        data = self.valid_data.copy()
        data.pop("name")
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        create_port_executor_mock.assert_not_called()

    def test_router_cannot_be_created_without_tenant(self, create_port_executor_mock):
        self.client.force_authenticate(self.fixture.owner)
        data = self.valid_data.copy()
        data.pop("tenant")
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        create_port_executor_mock.assert_not_called()


class BaseExternalGatewayTest(BaseRouterTest):
    def setUp(self):
        super().setUp()
        self.router = factories.RouterFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.external_network = factories.ExternalNetworkFactory(
            settings=self.fixture.settings,
            backend_id="ext-net-backend-id-1",
        )
        self.set_url = factories.RouterFactory.get_url(
            self.router, action="set_external_gateway"
        )
        self.remove_url = factories.RouterFactory.get_url(
            self.router, action="remove_external_gateway"
        )
        self.available_url = factories.RouterFactory.get_url(
            self.router, action="available_external_networks"
        )


@ddt
@mock.patch("waldur_openstack.executors.RouterSetExternalGatewayExecutor.execute")
class SetExternalGatewayPermissionTest(BaseExternalGatewayTest):
    @data("owner", "admin", "manager", "staff")
    def test_set_gateway_allowed(self, user, executor_mock):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.set_url,
            {"external_network_id": self.external_network.backend_id},
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_202_ACCEPTED,
            response.data,
        )

    def test_set_gateway_denied_for_member(self, executor_mock):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(
            self.set_url,
            {"external_network_id": self.external_network.backend_id},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        executor_mock.assert_not_called()

    def test_set_gateway_denied_for_user(self, executor_mock):
        """User without any role on the project gets 404 (resource not visible)."""
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(
            self.set_url,
            {"external_network_id": self.external_network.backend_id},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        executor_mock.assert_not_called()


@ddt
@mock.patch("waldur_openstack.executors.RouterRemoveExternalGatewayExecutor.execute")
class RemoveExternalGatewayPermissionTest(BaseExternalGatewayTest):
    def setUp(self):
        super().setUp()
        self.router.external_network_id = self.external_network.backend_id
        self.router.external_network_ref = self.external_network
        self.router.save()

    @data("owner", "admin", "manager", "staff")
    def test_remove_gateway_allowed(self, user, executor_mock):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.remove_url)
        self.assertEqual(
            response.status_code,
            status.HTTP_202_ACCEPTED,
            response.data,
        )

    def test_remove_gateway_denied_for_member(self, executor_mock):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(self.remove_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        executor_mock.assert_not_called()

    def test_remove_gateway_denied_for_user(self, executor_mock):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(self.remove_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        executor_mock.assert_not_called()


@mock.patch("waldur_openstack.executors.RouterSetExternalGatewayExecutor.execute")
class SetExternalGatewayGlobalNetworkTest(BaseExternalGatewayTest):
    def test_set_gateway_basic(self, executor_mock):
        response = self.client.post(
            self.set_url,
            {"external_network_id": self.external_network.backend_id},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.router.refresh_from_db()
        self.assertEqual(
            self.router.external_network_id, self.external_network.backend_id
        )
        self.assertEqual(self.router.external_network_ref, self.external_network)
        self.assertIsNone(self.router.enable_snat)
        executor_mock.assert_called_once()

    def test_set_gateway_with_invalid_network_id(self, executor_mock):
        response = self.client.post(
            self.set_url,
            {"external_network_id": "non-existent-network-id"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        executor_mock.assert_not_called()

    def test_set_gateway_updates_existing(self, executor_mock):
        # Set initial gateway
        self.router.external_network_id = "old-network-id"
        self.router.save()

        response = self.client.post(
            self.set_url,
            {"external_network_id": self.external_network.backend_id},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.router.refresh_from_db()
        self.assertEqual(
            self.router.external_network_id, self.external_network.backend_id
        )

    def test_set_gateway_snat_disabled_as_provider(self, executor_mock):
        """Provider (service settings customer owner) can disable SNAT on global network."""
        # fixture.owner is the customer owner which is also service_settings.customer owner
        response = self.client.post(
            self.set_url,
            {
                "external_network_id": self.external_network.backend_id,
                "enable_snat": False,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.router.refresh_from_db()
        self.assertFalse(self.router.enable_snat)

    def test_set_gateway_snat_disabled_as_project_admin_denied(self, executor_mock):
        """Project admin without provider role cannot disable SNAT on global network."""
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.post(
            self.set_url,
            {
                "external_network_id": self.external_network.backend_id,
                "enable_snat": False,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        executor_mock.assert_not_called()

    def test_set_gateway_fixed_ips_as_staff(self, executor_mock):
        """Staff can set fixed IPs on global network."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.set_url,
            {
                "external_network_id": self.external_network.backend_id,
                "external_fixed_ips": [{"ip_address": "10.0.0.5"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    def test_set_gateway_fixed_ips_as_project_admin_denied(self, executor_mock):
        """Project admin cannot set fixed IPs on global network."""
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.post(
            self.set_url,
            {
                "external_network_id": self.external_network.backend_id,
                "external_fixed_ips": [{"ip_address": "10.0.0.5"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        executor_mock.assert_not_called()

    def test_set_gateway_fixed_ips_missing_ip_address(self, executor_mock):
        """Fixed IP entries must contain ip_address field."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.set_url,
            {
                "external_network_id": self.external_network.backend_id,
                "external_fixed_ips": [{"subnet_id": "some-subnet"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        executor_mock.assert_not_called()


@mock.patch("waldur_openstack.executors.RouterSetExternalGatewayExecutor.execute")
class SetExternalGatewayRBACNetworkTest(BaseExternalGatewayTest):
    def setUp(self):
        super().setUp()
        # Create a source tenant/network with RBAC policy
        self.source_fixture = fixtures.OpenStackFixture()
        self.source_network = factories.NetworkFactory(
            tenant=self.source_fixture.tenant,
            project=self.source_fixture.project,
            service_settings=self.source_fixture.settings,
            backend_id="rbac-net-backend-id",
        )
        # Create RBAC policy granting external access to our router's tenant
        self.rbac_policy = factories.NetworkRBACPolicyFactory(
            network=self.source_network,
            target_tenant=self.fixture.tenant,
            policy_type=models.NetworkRBACPolicy.NetworkShareType.EXTERNAL,
        )

    def test_set_gateway_rbac_basic(self, executor_mock):
        """User can set RBAC network as gateway (basic, no SNAT control)."""
        response = self.client.post(
            self.set_url,
            {"external_network_id": self.source_network.backend_id},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.router.refresh_from_db()
        self.assertEqual(
            self.router.external_network_id, self.source_network.backend_id
        )
        # RBAC networks don't set external_network_ref
        self.assertIsNone(self.router.external_network_ref)

    def test_set_gateway_rbac_snat_disabled_with_both_tenant_admin(self, executor_mock):
        """User admin on both source and target projects can disable SNAT."""
        # Make fixture.owner admin on the source project too
        self.source_fixture.project.add_user(self.fixture.owner, ProjectRole.ADMIN)
        response = self.client.post(
            self.set_url,
            {
                "external_network_id": self.source_network.backend_id,
                "enable_snat": False,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    def test_set_gateway_rbac_snat_disabled_without_source_admin_denied(
        self, executor_mock
    ):
        """User without admin/manager on source project cannot disable SNAT."""
        response = self.client.post(
            self.set_url,
            {
                "external_network_id": self.source_network.backend_id,
                "enable_snat": False,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        executor_mock.assert_not_called()

    def test_set_gateway_rbac_fixed_ips_with_both_tenant_admin(self, executor_mock):
        """User admin on both projects can set fixed IPs for RBAC network."""
        self.source_fixture.project.add_user(self.fixture.owner, ProjectRole.ADMIN)
        response = self.client.post(
            self.set_url,
            {
                "external_network_id": self.source_network.backend_id,
                "external_fixed_ips": [{"ip_address": "10.0.0.5"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    def test_set_gateway_rbac_fixed_ips_without_source_admin_denied(
        self, executor_mock
    ):
        """User without admin on source cannot set fixed IPs."""
        response = self.client.post(
            self.set_url,
            {
                "external_network_id": self.source_network.backend_id,
                "external_fixed_ips": [{"ip_address": "10.0.0.5"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        executor_mock.assert_not_called()

    def test_set_gateway_rbac_network_without_policy_denied(self, executor_mock):
        """Network without RBAC policy is not available as external."""
        self.rbac_policy.delete()
        response = self.client.post(
            self.set_url,
            {"external_network_id": self.source_network.backend_id},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        executor_mock.assert_not_called()


@mock.patch("waldur_openstack.executors.RouterRemoveExternalGatewayExecutor.execute")
class RemoveExternalGatewayTest(BaseExternalGatewayTest):
    def test_remove_gateway_success(self, executor_mock):
        self.router.external_network_id = self.external_network.backend_id
        self.router.external_network_ref = self.external_network
        self.router.save()

        response = self.client.post(self.remove_url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        executor_mock.assert_called_once()

    def test_remove_gateway_no_gateway(self, executor_mock):
        response = self.client.post(self.remove_url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        executor_mock.assert_not_called()

    def test_remove_gateway_floating_ips_exist(self, executor_mock):
        self.router.external_network_id = self.external_network.backend_id
        self.router.external_network_ref = self.external_network
        self.router.save()

        factories.FloatingIPFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            backend_network_id=self.external_network.backend_id,
        )

        response = self.client.post(self.remove_url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        executor_mock.assert_not_called()


@mock.patch("waldur_openstack.executors.RouterSetExternalGatewayExecutor.execute")
@mock.patch("waldur_openstack.executors.RouterRemoveExternalGatewayExecutor.execute")
class ExternalGatewayAuditTest(BaseExternalGatewayTest):
    def _events(self):
        return logging_models.Event.objects.filter(
            event_type=EventType.OPENSTACK_ROUTER_UPDATED
        ).order_by("id")

    def test_set_gateway_emits_event(self, remove_mock, set_mock):
        response = self.client.post(
            self.set_url,
            {"external_network_id": self.external_network.backend_id},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        event = self._events().last()
        self.assertIsNotNone(event)
        self.assertEqual(
            event.context["new_external_network_id"],
            self.external_network.backend_id,
        )

    def test_remove_gateway_emits_event(self, remove_mock, set_mock):
        self.router.external_network_id = self.external_network.backend_id
        self.router.external_network_ref = self.external_network
        self.router.save()

        response = self.client.post(self.remove_url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        event = self._events().last()
        self.assertIsNotNone(event)
        self.assertEqual(
            event.context["old_external_network_id"],
            self.external_network.backend_id,
        )
        self.assertEqual(event.context["new_external_network_id"], "")


class AvailableExternalNetworksTest(BaseExternalGatewayTest):
    def test_lists_global_external_networks(self):
        response = self.client.get(self.available_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["backend_id"], self.external_network.backend_id
        )
        self.assertEqual(response.data[0]["source"], "global")

    def test_lists_rbac_external_networks(self):
        source_fixture = fixtures.OpenStackFixture()
        source_network = factories.NetworkFactory(
            tenant=source_fixture.tenant,
            project=source_fixture.project,
            service_settings=source_fixture.settings,
            backend_id="rbac-net-1",
        )
        factories.NetworkRBACPolicyFactory(
            network=source_network,
            target_tenant=self.fixture.tenant,
            policy_type=models.NetworkRBACPolicy.NetworkShareType.EXTERNAL,
        )

        response = self.client.get(self.available_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        sources = {r["source"] for r in response.data}
        self.assertEqual(sources, {"global", "rbac"})

    def test_deduplicates_networks(self):
        """If a network appears in both global and RBAC, only show once (global)."""
        # Create an RBAC policy for the same backend_id as the global external network
        source_fixture = fixtures.OpenStackFixture()
        source_network = factories.NetworkFactory(
            tenant=source_fixture.tenant,
            project=source_fixture.project,
            service_settings=source_fixture.settings,
            backend_id=self.external_network.backend_id,
        )
        factories.NetworkRBACPolicyFactory(
            network=source_network,
            target_tenant=self.fixture.tenant,
            policy_type=models.NetworkRBACPolicy.NetworkShareType.EXTERNAL,
        )

        response = self.client.get(self.available_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["source"], "global")


class RouterSerializerGatewayFieldsTest(BaseExternalGatewayTest):
    def test_gateway_fields_in_response(self):
        self.router.external_network_id = self.external_network.backend_id
        self.router.external_network_ref = self.external_network
        self.router.enable_snat = True
        self.router.external_fixed_ips = [{"ip_address": "10.0.0.5"}]
        self.router.save()

        url = factories.RouterFactory.get_url(self.router)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["external_network_id"], self.external_network.backend_id
        )
        self.assertTrue(response.data["has_external_gateway"])
        self.assertTrue(response.data["enable_snat"])
        self.assertEqual(
            response.data["external_fixed_ips"], [{"ip_address": "10.0.0.5"}]
        )

    def test_no_gateway_fields(self):
        url = factories.RouterFactory.get_url(self.router)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["external_network_id"], "")
        self.assertFalse(response.data["has_external_gateway"])
        self.assertIsNone(response.data["enable_snat"])


class PullTenantRoutersGatewayTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.external_network = factories.ExternalNetworkFactory(
            settings=self.fixture.settings,
            backend_id="ext-net-id-for-pull",
        )

    @mock.patch("waldur_openstack.backend.get_neutron_client")
    @mock.patch("waldur_openstack.backend.get_tenant_session")
    def test_pull_syncs_gateway_fields(self, mock_session, mock_neutron_client):
        neutron = mock_neutron_client.return_value
        neutron.list_routers.return_value = {
            "routers": [
                {
                    "id": "router-backend-1",
                    "name": "test-router",
                    "description": "",
                    "routes": [],
                    "external_gateway_info": {
                        "network_id": self.external_network.backend_id,
                        "enable_snat": True,
                        "external_fixed_ips": [
                            {
                                "ip_address": "192.168.1.1",
                                "subnet_id": "subnet-1",
                            }
                        ],
                    },
                }
            ]
        }
        neutron.list_ports.return_value = {"ports": []}

        from waldur_openstack.backend import OpenStackBackend

        backend = OpenStackBackend(self.fixture.settings)
        backend.pull_tenant_routers(self.tenant)

        router = models.Router.objects.get(
            tenant=self.tenant, backend_id="router-backend-1"
        )
        self.assertEqual(router.external_network_id, self.external_network.backend_id)
        self.assertEqual(router.external_network_ref, self.external_network)
        self.assertTrue(router.enable_snat)
        self.assertEqual(len(router.external_fixed_ips), 1)

    @mock.patch("waldur_openstack.backend.get_neutron_client")
    @mock.patch("waldur_openstack.backend.get_tenant_session")
    def test_pull_clears_gateway_fields_when_no_gateway(
        self, mock_session, mock_neutron_client
    ):
        # Create a router that currently has a gateway
        router = factories.RouterFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            backend_id="router-backend-2",
            external_network_id="old-net-id",
        )

        neutron = mock_neutron_client.return_value
        # When called with a specific router_backend_id, pull uses show_router
        neutron.show_router.return_value = {
            "router": {
                "id": "router-backend-2",
                "name": "test-router-2",
                "description": "",
                "routes": [],
                "external_gateway_info": None,
            }
        }
        neutron.list_ports.return_value = {"ports": []}

        from waldur_openstack.backend import OpenStackBackend

        backend = OpenStackBackend(self.fixture.settings)
        backend.pull_tenant_routers(self.tenant, "router-backend-2")

        router.refresh_from_db()
        self.assertEqual(router.external_network_id, "")
        self.assertIsNone(router.external_network_ref)
        self.assertIsNone(router.enable_snat)
