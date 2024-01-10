import json

import pkg_resources
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from waldur_paypal.backend import PaypalBackend, PayPalError
from waldur_paypal.helpers import override_paypal_settings
from waldur_paypal.tests import factories, fixtures


@override_paypal_settings(ENABLED=True)
class InvoiceWebhookTest(TestCase):
    # webhook simulator can be used for manual testing: https://developer.paypal.com/developer/webhooksSimulator/
    INVOICE_PAID_REQUEST_FILE_NAME = "invoice_paid_webhook.json"

    def setUp(self):
        self.url = reverse("paypal-invoice-webhook")
        self.fixture = fixtures.PayPalFixture()
        self.invoice = self.fixture.invoice

        self.CANCELLED = "INVOICING.INVOICE.CANCELLED"
        self.PAID = "INVOICING.INVOICE.PAID"
        self.REFUNDED = "INVOICING.INVOICE.REFUNDED"
        self.UPDATED = "INVOICING.INVOICE.UPDATED"
        self.CREATED = "INVOICING.INVOICE.CREATED"
        request = (
            pkg_resources.resource_stream(__name__, self.INVOICE_PAID_REQUEST_FILE_NAME)
            .read()
            .decode()
        )
        self.request_data = json.loads(request)

    def test_invoice_paid_event_updates_invoice_state(self):
        self.assertEqual(self.invoice.state, self.invoice.States.DRAFT)
        self.request_data["resource"]["id"] = self.invoice.backend_id

        # form data does not support 2d levels nesting
        response = self.client.post(
            self.url,
            data=json.dumps(self.request_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, self.invoice.States.PAID)

    def test_invoice_created_event_does_not_update_invoice_state(self):
        expected_state = self.invoice.state
        self.request_data["event_type"] = self.CREATED

        response = self.client.post(
            self.url,
            data=json.dumps(self.request_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_304_NOT_MODIFIED)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, expected_state)

    def test_payment_completed_event_does_not_update_invoice_state(self):
        expected_state = self.invoice.state
        self.request_data["event_type"] = "PAYMENT.CAPTURE.COMPLETED"

        response = self.client.post(
            self.url,
            data=json.dumps(self.request_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_304_NOT_MODIFIED)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, expected_state)


@override_paypal_settings(ENABLED=True)
class InvoiceBackendTest(TestCase):
    def setUp(self):
        self.invoice = factories.InvoiceFactory()
        self.invoice.backend_id = ""
        self.backend = PaypalBackend()

    def test_dont_send_invoice_in_backend_if_cost_is_zero(self):
        self.assertRaises(PayPalError, self.backend.create_invoice, self.invoice)
