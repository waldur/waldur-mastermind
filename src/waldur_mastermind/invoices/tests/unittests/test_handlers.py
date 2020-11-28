from decimal import Decimal
from unittest import mock

import pytz
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.utils import move_project
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.packages import models as packages_models
from waldur_mastermind.packages.tests import factories as packages_factories
from waldur_mastermind.packages.tests.utils import override_plugin_settings
from waldur_mastermind.support.tests import factories as support_factories
from waldur_mastermind.support.tests import fixtures as support_fixtures
from waldur_mastermind.support_invoices import utils as support_utils

from ... import models, registrators, utils
from .. import factories, fixtures


@override_plugin_settings(BILLING_ENABLED=True)
class AddNewOfferingDetailsToInvoiceTest(TransactionTestCase):
    def setUp(self):
        self.fixture = support_fixtures.SupportFixture()

    def test_invoice_is_created_on_offering_creation(self):
        offering = self.fixture.offering
        offering.state = offering.States.OK
        offering.save()
        self.assertEqual(models.Invoice.objects.count(), 1)
        invoice = models.Invoice.objects.first()
        self.assertTrue(invoice.generic_items.filter(scope=offering).exists())

    def test_invoice_is_not_created_for_pending_offering(self):
        issue = support_factories.IssueFactory(
            customer=self.fixture.customer, project=self.fixture.project
        )
        pending_offering = support_factories.OfferingFactory(issue=issue)

        offering = self.fixture.offering
        offering.state = offering.States.OK
        offering.save()

        self.assertEqual(models.Invoice.objects.count(), 1)
        invoice = models.Invoice.objects.first()
        self.assertTrue(invoice.generic_items.filter(scope=offering).exists())
        self.assertFalse(invoice.generic_items.filter(scope=pending_offering).exists())

    def test_existing_invoice_is_updated_on_offering_creation(self):
        start_date = timezone.datetime(2014, 2, 27, tzinfo=pytz.UTC)
        end_date = core_utils.month_end(start_date)
        usage_days = utils.get_full_days(start_date, end_date)

        with freeze_time(start_date):
            invoice = factories.InvoiceFactory(customer=self.fixture.customer)
            offering = self.fixture.offering
            offering.state = offering.States.OK
            offering.save()

        self.assertEqual(models.Invoice.objects.count(), 1)
        self.assertTrue(invoice.generic_items.filter(scope=offering).exists())
        expected_price = offering.unit_price * usage_days
        self.assertEqual(invoice.price, Decimal(expected_price))

    def test_existing_invoice_is_update_on_offering_creation_if_it_has_package_item_for_same_customer(
        self,
    ):
        start_date = timezone.datetime(2014, 2, 27, tzinfo=pytz.UTC)
        end_date = core_utils.month_end(start_date)
        usage_days = utils.get_full_days(start_date, end_date)

        with freeze_time(start_date):
            packages_factories.OpenStackPackageFactory(
                tenant__service_project_link__project__customer=self.fixture.customer
            )
            self.assertEqual(models.Invoice.objects.count(), 1)
            invoice = models.Invoice.objects.first()
            components_price = invoice.price
            offering = self.fixture.offering
            offering.state = offering.States.OK
            offering.save()
            self.assertEqual(models.Invoice.objects.count(), 1)

        self.assertTrue(invoice.generic_items.filter(scope=offering).exists())
        expected_price = offering.unit_price * usage_days + components_price
        self.assertEqual(invoice.price, Decimal(expected_price))


@override_plugin_settings(BILLING_ENABLED=True)
class UpdateInvoiceOnOfferingDeletionTest(TransactionTestCase):
    def setUp(self):
        self.fixture = support_fixtures.SupportFixture()

    def test_invoice_price_is_not_changed_after_a_while_if_offering_is_deleted(self):
        start_date = timezone.datetime(2014, 2, 27, tzinfo=pytz.UTC)
        end_date = core_utils.month_end(start_date)
        usage_days = utils.get_full_days(start_date, end_date)

        with freeze_time(start_date):
            offering = self.fixture.offering
            offering.state = offering.States.OK
            offering.save()
            self.assertEqual(models.Invoice.objects.count(), 1)
            invoice = models.Invoice.objects.first()
        with freeze_time(end_date):
            offering.delete()

        expected_price = offering.unit_price * usage_days
        self.assertEqual(invoice.price, Decimal(expected_price))

    def test_invoice_is_created_in_new_month_when_single_item_is_terminated(self):
        start_date = timezone.datetime(2014, 2, 27, tzinfo=pytz.UTC)
        next_month = timezone.datetime(2014, 3, 2, tzinfo=pytz.UTC)

        with freeze_time(start_date):
            offering = self.fixture.offering
            offering.state = offering.States.OK
            offering.save()
            self.assertEqual(models.Invoice.objects.count(), 1)
            invoice = models.Invoice.objects.first()
            packages_factories.OpenStackPackageFactory(
                tenant__service_project_link__project__customer=offering.project.customer
            )
            self.assertEqual(models.Invoice.objects.count(), 1)
            self.assertEqual(self.get_openstack_items(invoice).count(), 1)
            self.assertEqual(self.get_offering_items(invoice).count(), 1)

        with freeze_time(next_month):
            offering.delete()
            self.assertEqual(
                models.Invoice.objects.count(),
                2,
                "New invoice has to be created in new month.",
            )
            new_invoice = models.Invoice.objects.exclude(pk=invoice.pk).first()
            self.assertEqual(self.get_openstack_items(new_invoice).count(), 1)
            self.assertEqual(self.get_offering_items(new_invoice).count(), 1)
            self.assertEqual(
                self.get_offering_items(new_invoice).first().end, next_month
            )

    def get_openstack_items(self, invoice):
        model_type = ContentType.objects.get_for_model(packages_models.OpenStackPackage)
        return invoices_models.InvoiceItem.objects.filter(
            content_type=model_type, invoice=invoice
        )

    def get_offering_items(self, invoice):
        return support_utils.get_offering_items().filter(invoice=invoice)


@override_plugin_settings(BILLING_ENABLED=True)
class UpdateInvoiceOnOfferingStateChange(TransactionTestCase):
    def setUp(self):
        self.fixture = support_fixtures.SupportFixture()
        self.start_date = timezone.datetime(2014, 2, 7, tzinfo=pytz.UTC)

        with freeze_time(self.start_date):
            self.offering = self.fixture.offering
            self.offering.state = self.offering.States.OK
            self.offering.save()
            self.assertEqual(models.Invoice.objects.count(), 1)
            self.invoice = models.Invoice.objects.first()
            self.offering.state = self.offering.States.REQUESTED
            self.offering.save()
            self.assertEqual(models.Invoice.objects.count(), 1)

    def test_offering_item_is_terminated_when_its_state_changes(self):
        self.offering.state = self.offering.States.OK
        self.offering.save()

        termination_date = self.start_date + timezone.timedelta(days=2)
        deletion_date = termination_date + timezone.timedelta(days=2)
        usage_days = utils.get_full_days(self.start_date, termination_date)

        expected_price = self.offering.unit_price * usage_days
        with freeze_time(termination_date):
            self.offering.state = self.offering.States.TERMINATED
            self.offering.save()

        with freeze_time(deletion_date):
            self.offering.delete()

        self.assertEqual(self.invoice.generic_items.first().end, termination_date)
        self.assertEqual(self.invoice.price, Decimal(expected_price))


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


@override_plugin_settings(BILLING_ENABLED=True)
class ChangeProjectsCustomerTest(TransactionTestCase):
    def setUp(self):
        self.fixture = support_fixtures.SupportFixture()

    def test_delete_invoice_items_if_project_customer_has_been_changed(self):
        offering = self.fixture.offering
        offering.state = offering.States.OK
        offering.save()
        self.assertEqual(models.Invoice.objects.count(), 1)
        invoice = models.Invoice.objects.first()
        self.assertTrue(invoice.generic_items.filter(scope=offering).exists())

        new_customer = structure_factories.CustomerFactory()
        today = timezone.now()
        date = core_utils.month_start(today)
        (
            new_customer_invoice,
            create,
        ) = registrators.RegistrationManager.get_or_create_invoice(new_customer, date)
        self.assertFalse(
            new_customer_invoice.generic_items.filter(scope=offering).exists()
        )
        move_project(offering.project, new_customer)
        self.assertFalse(invoice.generic_items.filter(scope=offering).exists())
        self.assertTrue(
            new_customer_invoice.generic_items.filter(scope=offering).exists()
        )
