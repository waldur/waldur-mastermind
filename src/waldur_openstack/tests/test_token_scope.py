"""Creating routers and load balancers in a tenant is reserved to staff and the
tenant's owners, project admins and managers. A staff user's personal access
token without `STAFF.ACCESS` acts as the user without staff (#980).

Pools, pool members and health monitors share the check exercised here.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.tests.pat_utils import STAFF, use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack import models
from waldur_openstack.tests import factories, fixtures


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_openstack.executors.LoadBalancerCreateExecutor.execute")
@mock.patch("waldur_openstack.executors.RouterCreateExecutor.execute")
class TenantNetworkingTokenScopeTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.subnet = factories.SubNetFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
        )
        self.staff = structure_factories.UserFactory(is_staff=True)

    def create_router(self):
        return self.client.post(
            factories.RouterFactory.get_list_url(),
            {
                "name": "router",
                "tenant": factories.TenantFactory.get_url(self.tenant),
            },
        )

    def create_load_balancer(self):
        return self.client.post(
            factories.LoadBalancerFactory.get_list_url(),
            {
                "name": "lb",
                "tenant": factories.TenantFactory.get_url(self.tenant),
                "vip_subnet": factories.SubNetFactory.get_url(self.subnet),
            },
        )

    def assert_created(self, allowed):
        expected = status.HTTP_201_CREATED if allowed else status.HTTP_400_BAD_REQUEST
        response = self.create_router()
        self.assertEqual(response.status_code, expected, response.data)
        response = self.create_load_balancer()
        self.assertEqual(response.status_code, expected, response.data)
        self.assertEqual(
            models.Router.objects.filter(tenant=self.tenant).exists(), allowed
        )
        self.assertEqual(
            models.LoadBalancer.objects.filter(tenant=self.tenant).exists(), allowed
        )

    def test_staff_session_can_create(self, *_):
        self.client.force_authenticate(self.staff)
        self.assert_created(True)

    def test_staff_token_with_staff_scope_can_create(self, *_):
        use_pat(self.client, self.staff, STAFF)
        self.assert_created(True)

    def test_staff_token_without_staff_scope_cannot_create(self, *_):
        use_pat(self.client, self.staff)
        self.assert_created(False)

    def test_other_user_cannot_create(self, *_):
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assert_created(False)

    def test_tenant_owner_can_create(self, *_):
        self.client.force_authenticate(self.fixture.owner)
        self.assert_created(True)
