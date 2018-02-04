from ddt import ddt, data

from django.utils import timezone
from rest_framework import test, status

from waldur_core.structure.tests import factories as structure_factories, fixtures as structure_fixtures

from .utils import override_invoices_settings
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

    def test_updating_of_payments_data_updates_customers_data(self):
        EMAIL = 'new@customer.com'
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            'email': EMAIL,
        }
        self.client.put(factories.PaymentDetailsFactory.get_url(self.fixture.payment_details), data=payload)
        self.fixture.payment_details.customer.refresh_from_db()
        self.assertEqual(EMAIL, self.fixture.payment_details.customer.email)


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


class PaymentDetailFilterTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.PaymentDetailsFixture()

        self.customers = []
        self.customers.append(factories.PaymentDetailsFactory().customer)

        self.customers.append(structure_factories.CustomerFactory())

        self.customers.append(factories.PaymentDetailsFactory().customer)
        self.customers[-1].payment_details.accounting_start_date = timezone.now() + timezone.timedelta(days=1)
        self.customers[-1].payment_details.save()

    def _check_customer_(self, customer):
        """return the True if the customer is paying
        :param customer:
        :return: Bool
        """

        if customer.accounting_start_date > timezone.now():
            return False

        return True

    def test_filter_accounting_is_running_set_to_true(self):
        with override_invoices_settings(ENABLE_ACCOUNTING_START_DATE=True):
            self.client.force_authenticate(getattr(self.fixture, 'staff'))
            response = self.client.get(structure_factories.CustomerFactory.get_list_url(), {
                'accounting_is_running': True,
            })
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            count = len(filter(self._check_customer_, self.customers))
            self.assertEqual(len(response.data), count)

    def test_if_filter_accounting_is_running_set_to_false(self):
        with override_invoices_settings(ENABLE_ACCOUNTING_START_DATE=True):
            self.client.force_authenticate(getattr(self.fixture, 'staff'))
            response = self.client.get(structure_factories.CustomerFactory.get_list_url(), {
                'accounting_is_running': False,
            })
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            count = len(filter(lambda x: not self._check_customer_(x), self.customers))
            self.assertEqual(len(response.data), count)

    def test_if_filter_accounting_is_running_dont_set(self):
        with override_invoices_settings(ENABLE_ACCOUNTING_START_DATE=True):
            self.client.force_authenticate(getattr(self.fixture, 'staff'))
            response = self.client.get(structure_factories.CustomerFactory.get_list_url())
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data), len(self.customers))

    def test_if_ENABLE_ACCOUNTING_START_DATE_set_to_false(self):
        with override_invoices_settings(ENABLE_ACCOUNTING_START_DATE=False):
            self.client.force_authenticate(getattr(self.fixture, 'staff'))
            response = self.client.get(structure_factories.CustomerFactory.get_list_url())
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data), len(self.customers))
            response = self.client.get(structure_factories.CustomerFactory.get_list_url(), {
                'accounting_is_running': True,
            })
            self.assertEqual(len(response.data), len(self.customers))
            response = self.client.get(structure_factories.CustomerFactory.get_list_url(), {
                'accounting_is_running': False,
            })
            self.assertEqual(len(response.data), len(self.customers))
