import datetime

from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.invoices.models import Invoice, InvoiceItem
from waldur_mastermind.invoices.tasks import create_monthly_invoices
from waldur_mastermind.marketplace.billing import MarketplaceBillingService
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    BillingTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests.factories import (
    OfferingComponentFactory,
    OfferingFactory,
    PlanComponentFactory,
    PlanFactory,
    ResourceFactory,
)


class RemoteOfferingInvoiceLocalCalculationTest(test.APITestCase):
    """Test that invoices for remote offering resources are created via local calculation."""

    def setUp(self) -> None:
        self.fixture = ProjectFixture()
        self.offering = OfferingFactory(
            type=REMOTE_OFFERING,
            billable=True,
            secret_options={
                "api_url": "https://remote-waldur.com",
                "token": "valid_token",
                "customer_uuid": "customer-uuid",
            },
        )
        self.offering_component = OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            name="CPU",
            billing_type=BillingTypes.FIXED,
        )
        self.plan = PlanFactory(
            offering=self.offering,
            unit_price=0,
        )
        PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component,
            price=10,
            amount=1,
        )
        self.resource = ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            state=ResourceStates.CREATING,
        )
        self.resource.backend_id = "valid-backend-id"
        self.resource.save()
        self.customer = self.fixture.customer

    @freeze_time("2021-08-17")
    def test_invoice_is_created_when_resource_is_activated(self):
        """Invoice is created when remote resource transitions to OK state."""
        today = datetime.date.today()
        self.assertEqual(
            0,
            Invoice.objects.filter(
                customer__uuid=self.customer.uuid, year=today.year, month=today.month
            ).count(),
        )

        self.resource.set_state_ok()
        self.resource.save()

        self.assertEqual(
            1,
            Invoice.objects.filter(
                customer__uuid=self.customer.uuid, year=today.year, month=today.month
            ).count(),
        )

    @freeze_time("2021-08-01")
    def test_invoice_items_are_created_by_local_calculation(self):
        """Invoice items are created from local plan components, not pulled from remote."""
        self.resource.set_state_ok()
        self.resource.save()

        invoice = Invoice.objects.get(
            customer__uuid=self.customer.uuid, year=2021, month=8
        )
        items = InvoiceItem.objects.filter(
            resource__uuid=self.resource.uuid, invoice=invoice
        )
        self.assertEqual(items.count(), 1)
        item = items.first()
        self.assertEqual(item.unit_price, 10)
        # Full month on 1st: quantity=1, total=10
        self.assertEqual(item.quantity, 1)
        self.assertEqual(item.total, 10)

    @freeze_time("2021-08-17")
    def test_invoice_is_created_via_monthly_task(self):
        """create_monthly_invoices creates invoices for remote resources."""
        self.resource.set_state_ok()
        self.resource.save()

        # Create invoice for next month via monthly task
        with freeze_time("2021-09-01"):
            create_monthly_invoices()

        invoice = Invoice.objects.get(
            customer__uuid=self.customer.uuid, year=2021, month=9
        )
        items = InvoiceItem.objects.filter(
            resource__uuid=self.resource.uuid, invoice=invoice
        )
        self.assertEqual(items.count(), 1)

    @freeze_time("2021-10-01")
    def test_get_or_create_invoice_processes_remote_resources(self):
        """get_or_create_invoice processes remote resources when creating new invoice."""
        self.resource.set_state_ok()
        self.resource.save()

        today = timezone.now().date()
        invoice, _ = MarketplaceBillingService.get_or_create_invoice(
            self.customer, today
        )

        items = InvoiceItem.objects.filter(
            resource__uuid=self.resource.uuid, invoice=invoice
        )
        self.assertGreaterEqual(
            items.count(),
            1,
            "Remote resources should get invoice items from local calculation",
        )
