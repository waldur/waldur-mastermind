from rest_framework import status, test

from waldur_openstack.openstack.tests import factories, fixtures


class BaseRouterTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.owner)


class SetRoutesTest(BaseRouterTest):
    def setUp(self):
        super().setUp()
        self.router = factories.RouterFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.openstack_service_settings,
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
