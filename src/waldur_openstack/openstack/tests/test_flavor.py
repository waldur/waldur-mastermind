from ddt import data, ddt
from rest_framework import status, test

from waldur_openstack.openstack.tests import factories, fixtures


@ddt
class FlavorListRetrieveTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.flavor = factories.FlavorFactory(
            settings=self.fixture.openstack_service_settings
        )
        self.url = factories.FlavorFactory.get_list_url()

    @data("staff", "owner", "service_manager", "admin", "manager")
    def test_user_can_get_flavors_list(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
