from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tasks import create_monthly_invoices

from . import fixtures


@freeze_time("2020-11-01")
class InvoiceTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixtures = fixtures.ScriptFixture()
        self.resource = self.fixtures.resource
        self.resource.set_state_ok()
        self.resource.save()

    def test_handler_if_resource_has_been_created(self):
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        self.assertEqual(1, invoice.items.filter(resource=self.resource).count())

    @freeze_time("2020-11-02")
    def test_handler_if_resource_has_been_terminated(self):
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        item = invoice.items.get(resource=self.resource)
        self.resource.set_state_terminating()
        self.resource.save()
        self.resource.set_state_terminated()
        self.resource.save()
        item.refresh_from_db()
        self.assertEqual(item.end, timezone.now())

    @freeze_time("2020-12-01")
    def test_create_monthly_invoices(self):
        create_monthly_invoices()
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=12
        )
        self.assertEqual(1, invoice.items.filter(resource=self.resource).count())
