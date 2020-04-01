from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

from .. import models
from . import factories


@ddt
class ProfileRetrieveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.url = factories.PaymentProfileFactory.get_url(profile=self.profile)

    @data('owner', 'staff')
    def test_user_with_access_can_retrieve_customer_profile(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('manager', 'admin', 'user')
    def test_user_cannot_retrieve_customer_profile(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ProfileCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.url = factories.PaymentProfileFactory.get_list_url()

    def get_data(self):
        return {
            'organization': structure_factories.CustomerFactory.get_url(
                customer=self.fixture.customer
            ),
            'payment_type': models.PaymentType.INVOICES,
        }

    @data('owner', 'staff')
    def test_user_with_access_can_create_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.get_data())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('manager', 'admin', 'user')
    def test_user_cannot_create_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.get_data())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ProfileUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.url = factories.PaymentProfileFactory.get_url(profile=self.profile)

    @data('owner', 'staff')
    def test_user_with_access_can_update_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('manager', 'admin', 'user')
    def test_user_cannot_create_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ProfileDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.url = factories.PaymentProfileFactory.get_url(profile=self.profile)

    @data('owner', 'staff')
    def test_user_with_access_can_delete_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data('manager', 'admin', 'user')
    def test_user_cannot_delete_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
