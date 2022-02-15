from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tasks import create_monthly_invoices
from waldur_mastermind.marketplace.models import OfferingComponent
from waldur_mastermind.marketplace.tests.factories import ResourceFactory

from . import fixtures


@freeze_time('2020-11-01')
class InvoiceTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource

    def test_handler_if_resource_has_been_created(self):
        self.resource.set_state_ok()
        self.resource.save()
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        self.assertEqual(
            invoice.items.filter(
                resource_id=self.resource.id,
            ).count(),
            1,
        )

    @freeze_time('2020-11-02')
    def test_handler_if_resource_has_been_terminated(self):
        self.resource.set_state_ok()
        self.resource.save()
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer, year=2020, month=11
        )
        item = invoice.items.get(
            resource_id=self.resource.id,
        )
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
            invoice.items.filter(
                resource_id=self.resource.id,
            ).count(),
            1,
        )


@freeze_time('2020-11-01')
class TotalLimitTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.component = self.fixture.offering_component
        self.component.billing_type = OfferingComponent.BillingTypes.LIMIT
        self.component.limit_period = OfferingComponent.LimitPeriods.TOTAL
        self.component.save()
        self.resource = ResourceFactory(
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            project=self.fixture.project,
            limits={self.component.type: 10},
        )
        self.resource.set_state_ok()
        self.resource.save()

    def get_invoice_items(self, year=2020, month=11):
        invoice = invoices_models.Invoice.objects.get(
            customer=self.resource.project.customer,
            year=year,
            month=month,
        )
        return invoice.items.filter(
            details__offering_component_type=self.component.type,
            resource_id=self.resource.id,
        )

    def test_when_resource_provisioning_is_completed_invoice_item_is_created(self):
        items = self.get_invoice_items()
        self.assertEqual(items.count(), 1)

    def test_when_monthly_invoice_is_created_for_provisioned_resource_invoice_item_is_not_created(
        self,
    ):
        with freeze_time('2020-12-01'):
            create_monthly_invoices()
        items = self.get_invoice_items(year=2020, month=12)
        self.assertEqual(items.count(), 0)

    def test_when_limit_is_increased_invoice_item_is_created(self):
        self.resource.limits[self.component.type] = 20
        self.resource.save()

        items = self.get_invoice_items()
        self.assertEqual(items.count(), 2)
        self.assertTrue(items.last().unit_price > 0)
        self.assertEqual(items.last().quantity, 10)

    def test_when_limit_is_decreased_compensation_invoice_item_is_created(self):
        self.resource.limits[self.component.type] = 5
        self.resource.save()

        items = self.get_invoice_items()
        self.assertEqual(items.count(), 2)
        self.assertTrue(items.last().unit_price < 0)
        self.assertEqual(items.last().quantity, 5)
