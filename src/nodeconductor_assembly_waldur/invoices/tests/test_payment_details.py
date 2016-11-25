from rest_framework import test, status
from ddt import ddt, data

from nodeconductor.structure.tests import factories as structure_factories, fixtures as structure_fixtures

from . import factories, fixtures


@ddt
class PaymentDetailsRetrieveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PaymentDetailsFixture()

    @data('staff', 'owner')
    def test_can_retrieve_customer_payment_details(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(factories.PaymentDetailsFactory.get_url(self.fixture.payment_details))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('manager', 'admin', 'user')
    def test_cannot_retrieve_customer_payment_details(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(factories.PaymentDetailsFactory.get_url(self.fixture.payment_details))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class PaymentDetailsCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    @data('staff')
    def test_can_create_customer_payment_details(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {'customer': structure_factories.CustomerFactory.get_url(self.fixture.customer)}

        response = self.client.post(factories.PaymentDetailsFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('owner', 'manager', 'admin', 'user')
    def test_cannot_create_customer_payment_details(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {'customer': structure_factories.CustomerFactory.get_url(self.fixture.customer)}

        response = self.client.post(factories.PaymentDetailsFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class PaymentDetailsUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PaymentDetailsFixture()

    @data('staff')
    def test_can_update_customer_payment_details(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            'customer': structure_factories.CustomerFactory.get_url(self.fixture.customer),
            'email': 'test@customer.com',
        }

        response = self.client.put(factories.PaymentDetailsFactory.get_url(self.fixture.payment_details), data=payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('owner')
    def test_cannot_update_customer_payment_details(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            'customer': structure_factories.CustomerFactory.get_url(self.fixture.customer),
            'email': 'test@customer.com',
        }

        response = self.client.put(factories.PaymentDetailsFactory.get_url(self.fixture.payment_details), data=payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class PaymentDetailsDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PaymentDetailsFixture()

    @data('staff')
    def test_can_delete_customer_payment_details(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(factories.PaymentDetailsFactory.get_url(self.fixture.payment_details))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data('owner')
    def test_cannot_update_customer_payment_details(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(factories.PaymentDetailsFactory.get_url(self.fixture.payment_details))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
