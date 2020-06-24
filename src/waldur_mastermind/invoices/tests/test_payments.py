from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures

from . import factories


@ddt
class PaymentRetrieveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.payment = factories.PaymentFactory(profile=self.profile)
        self.url = factories.PaymentFactory.get_url(payment=self.payment)

    @data(
        'staff', 'owner',
    )
    def test_user_with_access_can_retrieve_payment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('manager', 'admin', 'user')
    def test_user_cannot_retrieve_customer_profile(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class PaymentCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.url = factories.PaymentFactory.get_list_url()

    def get_data(self):
        return {
            'profile': factories.PaymentProfileFactory.get_url(profile=self.profile),
            'sum': 200,
            'date_of_payment': '2000-01-01',
        }

    @data('staff',)
    def test_user_with_access_can_create_payments(self, user):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.url = factories.PaymentFactory.get_list_url()

    @data('owner', 'manager', 'admin', 'user', 'global_support')
    def test_user_cannot_create_payments(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.get_data())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class PaymentUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.payment = factories.PaymentFactory(profile=self.profile)
        self.url = factories.PaymentFactory.get_url(payment=self.payment)

    @data('staff',)
    def test_user_with_access_can_update_payment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('owner', 'manager', 'admin', 'user', 'global_support')
    def test_user_cannot_create_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class PaymentDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.payment = factories.PaymentFactory(profile=self.profile)
        self.url = factories.PaymentFactory.get_url(payment=self.payment)

    @data('staff',)
    def test_user_with_access_can_delete_payment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data('owner', 'manager', 'admin', 'user', 'global_support')
    def test_user_cannot_delete_payment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
