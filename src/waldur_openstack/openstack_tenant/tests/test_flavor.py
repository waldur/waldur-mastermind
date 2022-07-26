from ddt import data, ddt
from rest_framework import status, test

from waldur_openstack.openstack_tenant import models
from waldur_openstack.openstack_tenant.tests import factories, fixtures


@ddt
class FlavorListRetrieveTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.flavor = factories.FlavorFactory(
            state=models.Flavor.States.OK,
            settings=self.fixture.openstack_tenant_service_settings,
        )
        self.url = factories.FlavorFactory.get_list_url()

    @data('staff', 'owner', 'service_manager', 'admin', 'manager', 'user')
    def test_user_can_get_flavors_list(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


@ddt
class FlavorCreateTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.url = factories.FlavorFactory.get_list_url()
        self.data = {
            'name': 'flavor-1',
            'settings': factories.OpenStackTenantServiceSettingsFactory.get_url(
                self.fixture.openstack_tenant_service_settings
            ),
            'ram': 512,
            'disk': 1024,
            'cores': 1,
        }

    @data('staff', 'owner', 'service_manager')
    def test_user_can_create_flavor(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('admin', 'manager', 'user')
    def test_user_can_not_create_flavor(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class FlavorDeleteTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.flavor = factories.FlavorFactory(
            state=models.Flavor.States.OK,
            settings=self.fixture.openstack_tenant_service_settings,
        )
        self.url = factories.FlavorFactory.get_url(self.flavor)

    @data('staff', 'owner', 'service_manager')
    def test_user_can_delete_flavor(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @data('admin', 'manager', 'user')
    def test_user_can_not_delete_flavor(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
