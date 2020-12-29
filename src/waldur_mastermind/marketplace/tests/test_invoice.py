from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tasks import create_monthly_invoices

from . import fixtures


@freeze_time('2020-11-01')
class InvoiceTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixtures = fixtures.MarketplaceFixture()
        self.resource = self.fixtures.resource
        self.resource.set_state_ok()
        self.resource.save()
        self.content_type = ContentType.objects.get_for_model(self.resource)

    def test_handler_if_resource_has_been_created(self):
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        self.assertEqual(
            invoice.items.filter(
                object_id=self.resource.id, content_type=self.content_type
            ).count(),
            1,
        )

    @freeze_time('2020-11-02')
    def test_handler_if_resource_has_been_terminated(self):
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        item = invoice.items.get(
            object_id=self.resource.id, content_type=self.content_type
        )
        self.resource.set_state_terminating()
        self.resource.save()
        self.resource.set_state_terminated()
        self.resource.save()
        item.refresh_from_db()
        self.assertEqual(item.end, timezone.now())

    @freeze_time('2020-12-01')
    def test_create_monthly_invoices(self):
        create_monthly_invoices()
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=12
        )
        self.assertEqual(
            invoice.items.filter(
                object_id=self.resource.id, content_type=self.content_type
            ).count(),
            1,
        )
