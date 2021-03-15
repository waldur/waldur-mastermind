from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tasks import create_monthly_invoices
from waldur_mastermind.marketplace_openstack import TENANT_TYPE

from . import fixtures


@freeze_time('2020-11-01')
class InvoiceTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixtures = fixtures.MarketplaceFixture()
        self.resource = self.fixtures.resource

    def test_handler_if_resource_has_been_created(self):
        self.resource.set_state_ok()
        self.resource.save()
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        self.assertEqual(
            invoice.items.filter(resource_id=self.resource.id,).count(), 1,
        )

    @freeze_time('2020-11-02')
    def test_handler_if_resource_has_been_terminated(self):
        self.resource.set_state_ok()
        self.resource.save()
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        item = invoice.items.get(resource_id=self.resource.id,)
        self.resource.set_state_terminating()
        self.resource.save()
        self.resource.set_state_terminated()
        self.resource.save()
        item.refresh_from_db()
        self.assertEqual(item.end, timezone.now())

    @freeze_time('2020-12-01')
    def test_create_monthly_invoices(self):
        self.resource.set_state_ok()
        self.resource.save()
        create_monthly_invoices()
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=12
        )
        self.assertEqual(
            invoice.items.filter(resource_id=self.resource.id,).count(), 1,
        )

    def test_create_invoice_if_other_type_resource_exists(self):
        new_fixture = fixtures.MarketplaceFixture()
        new_fixture.offering.type = TENANT_TYPE
        new_fixture.offering.save()
        new_fixture.resource.project = self.resource.project
        new_fixture.resource.set_state_ok()
        new_fixture.resource.save()

        with freeze_time('2020-12-01'):
            self.resource.set_state_ok()
            self.resource.save()
            invoice = invoices_models.Invoice.objects.get(
                customer=self.resource.project.customer, year=2020, month=12
            )
            self.assertEqual(
                invoice.items.count(), 2,
            )
