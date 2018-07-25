import decimal
import mock

from rest_framework import test, status

from waldur_core.structure.tests import fixtures as structure_fixtures, factories as structure_factories
from waldur_paypal.backend import PaypalPayment, PayPalError
from waldur_paypal.helpers import override_paypal_settings
from waldur_paypal.models import Payment

from . import factories


@override_paypal_settings(ENABLED=True)
class BasePaymentTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.other = structure_factories.UserFactory()

        self.valid_request = {
            'amount': decimal.Decimal('9.99'),
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
            'return_url': 'http://example.com/return/',
            'cancel_url': 'http://example.com/cancel/'
        }

        self.valid_response = {
            'approval_url': 'https://www.paypal.com/webscr?cmd=_express-checkout&token=EC-60U79048BN7719609',
            'payer_id': '7E7MGXCWTTKK2',
            'token': 'EC-60U79048BN7719609'
        }


class PaymentCreateTest(BasePaymentTest):

    def create_payment(self, user, fail=False):
        with mock.patch('waldur_paypal.backend.PaypalBackend') as backend:
            if fail:
                backend().make_payment.side_effect = PayPalError()
            else:
                backend().make_payment.return_value = PaypalPayment(
                    payment_id='PAY-6RV70583SB702805EKEYSZ6Y',
                    approval_url=self.valid_response['approval_url'],
                    token=self.valid_response['token'])

            self.client.force_authenticate(user)
            return self.client.post(factories.PaypalPaymentFactory.get_list_url(), data=self.valid_request)

    def test_staff_can_create_payment_for_any_customer(self):
        response = self.create_payment(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.valid_response['approval_url'], response.data['approval_url'])

    def test_user_can_create_payment_for_owned_customer(self):
        response = self.create_payment(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_user_can_not_create_payment_for_other_customer(self):
        response = self.create_payment(self.other)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_when_backend_fails_error_returned(self):
        response = self.create_payment(self.fixture.owner, fail=True)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @override_paypal_settings(ENABLED=False)
    def test_failed_dependency_raised_if_extension_is_disabled(self):
        response = self.create_payment(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)


class PaymentApprovalTest(BasePaymentTest):

    def approve_payment(self, user, amount=None, fail=False):
        payment = factories.PaypalPaymentFactory(
            customer=self.customer,
            state=Payment.States.CREATED,
            amount=amount or 100.0)

        with mock.patch('waldur_paypal.backend.PaypalBackend') as backend:
            if fail:
                backend().approve_payment.side_effect = PayPalError()

            self.client.force_authenticate(user)
            return self.client.post(factories.PaypalPaymentFactory.get_list_url() + 'approve/', data={
                'payment_id': payment.backend_id,
                'payer_id': self.valid_response['payer_id'],
                'token': payment.token
            })

    def test_staff_can_approve_any_payment(self):
        response = self.approve_payment(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_user_can_approve_payment_for_owned_customer(self):
        response = self.approve_payment(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_user_can_not_approve_payment_for_other_customer(self):
        response = self.approve_payment(self.other)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PaymentCancellationTest(BasePaymentTest):

    def cancel_payment(self, user):
        self.client.force_authenticate(user)
        payment = factories.PaypalPaymentFactory(customer=self.customer, state=Payment.States.CREATED)
        return self.client.post(factories.PaypalPaymentFactory.get_list_url() + 'cancel/', data={
            'token': payment.token
        })

    def test_staff_can_cancel_any_payment(self):
        response = self.cancel_payment(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_user_can_cancel_payment_for_owned_customer(self):
        response = self.cancel_payment(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_can_not_cancel_payment_for_other_customer(self):
        response = self.cancel_payment(self.other)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_paypal_settings(ENABLED=False)
    def test_failed_dependency_raised_if_extension_is_disabled(self):
        response = self.cancel_payment(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)
