from unittest import mock

from django.conf import settings
from django.test import TransactionTestCase
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.utils import move_project
from waldur_mastermind.invoices import models, registrators
from waldur_mastermind.invoices.tests import factories, fixtures


class EmitInvoiceCreatedOnStateChange(TransactionTestCase):
    @mock.patch('waldur_mastermind.invoices.signals.invoice_created')
    def test_invoice_created_signal_is_emitted_on_monthly_invoice_creation(
        self, invoice_created_mock
    ):
        fixture = fixtures.InvoiceFixture()
        invoice = fixture.invoice
        invoice.set_created()

        new_invoice = models.Invoice.objects.get(
            customer=fixture.customer, state=models.Invoice.States.CREATED
        )
        invoice_created_mock.send.assert_called_once_with(
            invoice=new_invoice,
            sender=models.Invoice,
            issuer_details=settings.WALDUR_INVOICES['ISSUER_DETAILS'],
        )


class UpdateInvoiceCurrentCostTest(TransactionTestCase):
    def setUp(self):
        super(UpdateInvoiceCurrentCostTest, self).setUp()
        self.project = structure_factories.ProjectFactory()
        self.invoice = factories.InvoiceFactory(customer=self.project.customer)

    def create_invoice_item(self):
        return factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            unit_price=100,
            quantity=1,
            unit=models.InvoiceItem.Units.QUANTITY,
        )

    def test_when_invoice_item_is_created_current_cost_is_updated(self):
        self.create_invoice_item()
        self.invoice.refresh_from_db()
        self.assertEqual(100, self.invoice.current_cost)

    def test_default_tax_percent_is_used_on_invoice_creation(self):
        customer = structure_factories.CustomerFactory(default_tax_percent=20)
        invoice = factories.InvoiceFactory(customer=customer)
        self.assertEqual(invoice.tax_percent, customer.default_tax_percent)

    def test_when_invoice_item_is_updated_current_cost_is_updated(self):
        invoice_item = self.create_invoice_item()

        invoice_item.quantity = 2
        invoice_item.save()

        self.invoice.refresh_from_db()
        self.assertEqual(200, self.invoice.current_cost)

    def test_when_invoice_item_is_deleted_current_cost_is_updated(self):
        invoice_item = self.create_invoice_item()
        invoice_item.delete()

        self.invoice.refresh_from_db()
        self.assertEqual(0, self.invoice.current_cost)


class MoveProjectInvoiceTest(TransactionTestCase):
    def test_delete_invoice_items_if_project_customer_has_been_changed(self):
        fixture = fixtures.InvoiceFixture()
        invoice_item = fixture.invoice_item
        invoice_item.resource = fixture.resource
        invoice_item.save()

        new_customer = structure_factories.CustomerFactory()
        today = timezone.now()
        date = core_utils.month_start(today)
        (
            new_customer_invoice,
            create,
        ) = registrators.RegistrationManager.get_or_create_invoice(new_customer, date)
        self.assertFalse(
            new_customer_invoice.items.filter(resource=fixture.resource).exists()
        )
        move_project(fixture.resource.project, new_customer)
        self.assertFalse(
            fixture.invoice.items.filter(resource=fixture.resource).exists()
        )
        self.assertTrue(
            new_customer_invoice.items.filter(resource=fixture.resource).exists()
        )
