"""A personal access token is held to its bindings at a router's gateway.

Provider-internal external networks and the advanced gateway options belong
to the provider's organization and to the owner of an RBAC-shared network. A
token bound to a consumer project must not use its user's roles there.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.enums import CoreStates
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.tests.pat_utils import STAFF, use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack import models

from . import factories, fixtures

GATEWAY = PermissionEnum.CAN_MANAGE_OPENSTACK_ROUTER_GATEWAY.value


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_openstack.executors.RouterSetExternalGatewayExecutor.execute")
class ProviderInternalNetworkTest(test.APITestCase):
    """The provider's owner also administers the consumer project."""

    def setUp(self):
        CustomerRole.OWNER.add_permission(
            PermissionEnum.CAN_MANAGE_OPENSTACK_ROUTER_GATEWAY
        )
        ProjectRole.ADMIN.add_permission(
            PermissionEnum.CAN_MANAGE_OPENSTACK_ROUTER_GATEWAY
        )
        self.provider = structure_factories.CustomerFactory()
        self.user = structure_factories.UserFactory()
        self.provider.add_user(self.user, CustomerRole.OWNER)
        self.project = structure_factories.ProjectFactory()
        self.project.add_user(self.user, ProjectRole.ADMIN)

        settings = factories.SettingsFactory(
            customer=self.provider, shared=True, state=CoreStates.OK
        )
        self.tenant = factories.TenantFactory(
            service_settings=settings, project=self.project, state=CoreStates.OK
        )
        self.router = factories.RouterFactory(
            tenant=self.tenant, project=self.project, service_settings=settings
        )
        self.internal = factories.ExternalNetworkFactory(
            settings=settings, backend_id="provider-internal", is_shared=False
        )

    def set_gateway(self):
        return self.client.post(
            factories.RouterFactory.get_url(self.router, "set_external_gateway"),
            {"external_network_id": self.internal.backend_id, "enable_snat": False},
        )

    def available(self):
        response = self.client.get(
            factories.RouterFactory.get_url(self.router, "available_external_networks")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {network["backend_id"] for network in response.data}

    def test_token_bound_to_the_consumer_project_cannot(self, execute):
        use_pat(self.client, self.user, GATEWAY, bindings=[self.project])

        self.assertEqual(self.set_gateway().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn(self.internal.backend_id, self.available())
        execute.assert_not_called()

    def test_unbound_token_can(self, execute):
        use_pat(self.client, self.user, GATEWAY)

        response = self.set_gateway()

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertIn(self.internal.backend_id, self.available())

    def listed(self):
        response = self.client.get(factories.ExternalNetworkFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {network["backend_id"] for network in response.data}

    def test_the_external_network_list_hides_it_from_a_bound_token(self, execute):
        use_pat(self.client, self.user, GATEWAY, bindings=[self.project])
        self.assertNotIn(self.internal.backend_id, self.listed())

        use_pat(self.client, self.user, GATEWAY)
        self.assertIn(self.internal.backend_id, self.listed())

    def test_a_token_bound_to_the_service_provider_can(self, execute):
        provider = marketplace_factories.ServiceProviderFactory(customer=self.provider)
        use_pat(self.client, self.user, GATEWAY, bindings=[provider])

        self.assertIn(self.internal.backend_id, self.listed())

    @mock.patch("waldur_openstack.executors.TenantPushQuotasExecutor.execute")
    def test_tenant_quotas_take_a_service_provider_binding(self, push, execute):
        provider = marketplace_factories.ServiceProviderFactory(customer=self.provider)
        use_pat(self.client, self.user, GATEWAY, bindings=[provider])

        response = self.client.post(
            factories.TenantFactory.get_url(self.tenant, "set_quotas"),
            {"instances": 10},
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    @mock.patch("waldur_openstack.executors.TenantPushQuotasExecutor.execute")
    def test_tenant_quotas_take_the_provider_within_the_binding(self, push, execute):
        url = factories.TenantFactory.get_url(self.tenant, "set_quotas")

        use_pat(self.client, self.user, GATEWAY, bindings=[self.project])
        refused = self.client.post(url, {"instances": 10})
        use_pat(self.client, self.user, GATEWAY)
        accepted = self.client.post(url, {"instances": 10})

        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(accepted.status_code, status.HTTP_202_ACCEPTED, accepted.data)
        push.assert_called_once()


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_openstack.executors.RouterSetExternalGatewayExecutor.execute")
class SharedExternalNetworkTest(test.APITestCase):
    """Advanced options on a network another project shared as external."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.router = factories.RouterFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        source = fixtures.OpenStackFixture()
        self.network = factories.NetworkFactory(
            tenant=source.tenant,
            project=source.project,
            service_settings=source.settings,
            backend_id="shared-as-external",
        )
        factories.NetworkRBACPolicyFactory(
            network=self.network,
            target_tenant=self.fixture.tenant,
            policy_type=models.NetworkRBACPolicy.NetworkShareType.EXTERNAL,
        )
        self.staff = structure_factories.UserFactory(is_staff=True)

    def set_gateway(self):
        return self.client.post(
            factories.RouterFactory.get_url(self.router, "set_external_gateway"),
            {"external_network_id": self.network.backend_id, "enable_snat": False},
        )

    def test_staff_token_bound_to_the_router_project_cannot(self, execute):
        use_pat(
            self.client, self.staff, STAFF, GATEWAY, bindings=[self.fixture.project]
        )

        self.assertEqual(self.set_gateway().status_code, status.HTTP_400_BAD_REQUEST)
        execute.assert_not_called()

    def test_unbound_staff_token_can(self, execute):
        use_pat(self.client, self.staff, STAFF, GATEWAY)

        response = self.set_gateway()

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_openstack.executors.RouterSetExternalGatewayExecutor.execute")
class StaffTokenTest(test.APITestCase):
    """Staff act in every project, but a token only within its bindings."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.router = factories.RouterFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.internal = factories.ExternalNetworkFactory(
            settings=self.fixture.settings,
            backend_id="provider-internal",
            is_shared=False,
        )
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.elsewhere = structure_factories.ProjectFactory()

    def bind(self, *bindings):
        use_pat(self.client, self.staff, STAFF, GATEWAY, bindings=list(bindings))

    def set_gateway(self):
        return self.client.post(
            factories.RouterFactory.get_url(self.router, "set_external_gateway"),
            {"external_network_id": self.internal.backend_id, "enable_snat": False},
        )

    def test_a_token_bound_elsewhere_cannot_set_the_gateway(self, execute):
        self.bind(self.elsewhere)

        self.assertEqual(self.set_gateway().status_code, status.HTTP_403_FORBIDDEN)
        execute.assert_not_called()

    def test_a_token_bound_to_the_router_project_can(self, execute):
        self.bind(self.fixture.project)

        response = self.set_gateway()

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    @mock.patch("waldur_openstack.executors.TenantPushQuotasExecutor.execute")
    def test_tenant_quotas_take_a_binding_on_the_tenant_project(self, push, execute):
        url = factories.TenantFactory.get_url(self.fixture.tenant, "set_quotas")

        self.bind(self.elsewhere)
        refused = self.client.post(url, {"instances": 10})
        self.bind(self.fixture.project)
        accepted = self.client.post(url, {"instances": 10})

        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(accepted.status_code, status.HTTP_202_ACCEPTED, accepted.data)

    @mock.patch(
        "waldur_openstack.backend.OpenStackBackend.get_console_url",
        return_value="url",
    )
    def test_the_console_takes_a_binding_on_the_instance_project(
        self, console, execute
    ):
        url = factories.InstanceFactory.get_url(self.fixture.instance, "console")

        self.bind(self.elsewhere)
        refused = self.client.get(url)
        self.bind(self.fixture.project)
        accepted = self.client.get(url)

        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(accepted.status_code, status.HTTP_200_OK, accepted.data)
        console.assert_called_once()
