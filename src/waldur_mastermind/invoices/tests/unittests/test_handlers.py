import decimal
from calendar import monthrange
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
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices import models, registrators, utils
from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.packages import models as packages_models
from waldur_mastermind.packages.tests import factories as packages_factories
from waldur_mastermind.packages.tests.utils import override_plugin_settings
from waldur_mastermind.support.tests import fixtures as support_fixtures


class BaseInvoiceTest(TransactionTestCase):
    def setUp(self):
        self.fixture = support_fixtures.SupportFixture()
        self.marketplace_offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME
        )
        offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.marketplace_offering
        )
        self.plan = marketplace_factories.PlanFactory(
            unit=marketplace_models.Plan.Units.PER_MONTH
        )
        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.plan, component=offering_component, price=7,
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.marketplace_offering,
            project=self.fixture.project,
            plan=self.plan,
        )

    def get_factor(self, start_date, usage_days):
        month_days = monthrange(start_date.year, start_date.month)[1]
        return quantize_price(decimal.Decimal(usage_days) / month_days)


@override_plugin_settings(BILLING_ENABLED=True)
class AddNewOfferingDetailsToInvoiceTest(BaseInvoiceTest):
    def test_invoice_is_created_on_offering_creation(self):
        self.resource.set_state_ok()
        self.resource.save()
        self.assertEqual(models.Invoice.objects.count(), 1)
        invoice = models.Invoice.objects.first()
        self.assertTrue(invoice.items.filter(scope=self.resource).exists())

    def test_invoice_is_not_created_for_pending_offering(self):
        pending_resource = marketplace_factories.ResourceFactory(
            offering=self.marketplace_offering,
            project=self.fixture.project,
            plan=self.plan,
        )

        self.resource.set_state_ok()
        self.resource.save()

        self.assertEqual(models.Invoice.objects.count(), 1)
        invoice = models.Invoice.objects.first()
        self.assertTrue(invoice.items.filter(scope=self.resource).exists())
        self.assertFalse(invoice.items.filter(scope=pending_resource).exists())

    def test_existing_invoice_is_updated_on_offering_creation(self):
        start_date = timezone.datetime(2014, 2, 27, tzinfo=pytz.UTC)
        end_date = core_utils.month_end(start_date)
        usage_days = utils.get_full_days(start_date, end_date)
        month_days = monthrange(start_date.year, start_date.month)[1]
        factor = quantize_price(decimal.Decimal(usage_days) / month_days)

        with freeze_time(start_date):
            invoice = factories.InvoiceFactory(customer=self.fixture.customer)
            self.resource.set_state_ok()
            self.resource.save()

        self.assertEqual(models.Invoice.objects.count(), 1)
        self.assertTrue(invoice.items.filter(scope=self.resource).exists())
        expected_price = self.plan_component.price * factor
        self.assertEqual(invoice.price, Decimal(expected_price))

    def test_existing_invoice_is_updated_on_offering_creation_if_it_has_package_item_for_same_customer(
        self,
    ):
        start_date = timezone.datetime(2014, 2, 27, tzinfo=pytz.UTC)
        end_date = core_utils.month_end(start_date)
        usage_days = utils.get_full_days(start_date, end_date)
        month_days = monthrange(start_date.year, start_date.month)[1]
        factor = quantize_price(decimal.Decimal(usage_days) / month_days)

        with freeze_time(start_date):
            packages_factories.OpenStackPackageFactory(
                tenant__service_project_link__project__customer=self.fixture.customer
            )
            self.assertEqual(models.Invoice.objects.count(), 1)
            invoice = models.Invoice.objects.first()
            components_price = invoice.price
            self.resource.set_state_ok()
            self.resource.save()
            self.assertEqual(models.Invoice.objects.count(), 1)

        self.assertTrue(invoice.items.filter(scope=self.resource).exists())
        expected_price = self.plan_component.price * factor + components_price
        self.assertEqual(invoice.price, Decimal(expected_price))


@override_plugin_settings(BILLING_ENABLED=True)
class UpdateInvoiceOnOfferingDeletionTest(BaseInvoiceTest):
    def test_invoice_price_is_not_changed_after_a_while_if_offering_is_deleted(self):
        start_date = timezone.datetime(2014, 2, 27, tzinfo=pytz.UTC)
        end_date = core_utils.month_end(start_date)
        usage_days = utils.get_full_days(start_date, end_date)
        month_days = monthrange(start_date.year, start_date.month)[1]
        factor = quantize_price(decimal.Decimal(usage_days) / month_days)

        with freeze_time(start_date):
            self.resource.set_state_ok()
            self.resource.save()
            self.assertEqual(models.Invoice.objects.count(), 1)
            invoice = models.Invoice.objects.first()

        with freeze_time(end_date):
            self.resource.set_state_terminating()
            self.resource.save()
            self.resource.set_state_terminated()
            self.resource.save()

        expected_price = self.plan_component.price * factor
        self.assertEqual(invoice.price, Decimal(expected_price))

    def test_invoice_is_created_in_new_month_when_single_item_is_terminated(self):
        start_date = timezone.datetime(2014, 2, 27, tzinfo=pytz.UTC)
        next_month = timezone.datetime(2014, 3, 2, tzinfo=pytz.UTC)

        with freeze_time(start_date):
            self.resource.set_state_ok()
            self.resource.save()
            self.assertEqual(models.Invoice.objects.count(), 1)
            invoice = models.Invoice.objects.first()
            packages_factories.OpenStackPackageFactory(
                tenant__service_project_link__project__customer=self.resource.project.customer
            )
            self.assertEqual(models.Invoice.objects.count(), 1)
            self.assertEqual(self.get_openstack_items(invoice).count(), 1)
            self.assertEqual(self.get_offering_items(invoice).count(), 1)

        with freeze_time(next_month):
            new_invoice, _ = registrators.RegistrationManager.get_or_create_invoice(
                self.resource.project.customer, next_month
            )
            self.resource.set_state_terminating()
            self.resource.save()
            self.resource.set_state_terminated()
            self.resource.save()
            self.assertEqual(self.get_openstack_items(new_invoice).count(), 1)
            self.assertEqual(self.get_offering_items(new_invoice).count(), 1)
            self.assertEqual(
                self.get_offering_items(new_invoice).first().end, next_month
            )

    def get_openstack_items(self, invoice):
        model_type = ContentType.objects.get_for_model(packages_models.OpenStackPackage)
        return models.InvoiceItem.objects.filter(
            content_type=model_type, invoice=invoice
        )

    def get_offering_items(self, invoice):
        resource_model_type = ContentType.objects.get_for_model(
            marketplace_models.Resource
        )
        resources_ids = marketplace_models.Resource.objects.filter(
            offering__type=PLUGIN_NAME
        ).values_list('id', flat=True)
        return models.InvoiceItem.objects.filter(
            invoice=invoice,
            content_type=resource_model_type,
            object_id__in=resources_ids,
        )


@override_plugin_settings(BILLING_ENABLED=True)
class UpdateInvoiceOnOfferingStateChange(BaseInvoiceTest):
    def setUp(self):
        super().setUp()
        self.start_date = timezone.datetime(2014, 2, 7, tzinfo=pytz.UTC)

    def test_invoice_item_is_terminated_when_resource_state_is_changed(self):
        with freeze_time(self.start_date):
            self.resource.set_state_ok()
            self.resource.save()
            self.assertEqual(models.Invoice.objects.count(), 1)
            self.invoice = models.Invoice.objects.first()

        termination_date = self.start_date + timezone.timedelta(days=2)
        usage_days = (termination_date - self.start_date).days + 1
        factor = self.get_factor(self.start_date, usage_days)

        expected_price = self.plan_component.price * factor
        with freeze_time(termination_date):
            self.resource.set_state_terminating()
            self.resource.save()
            self.resource.set_state_terminated()
            self.resource.save()
            self.assertEqual(self.invoice.items.first().end, termination_date)

        self.assertEqual(self.invoice.items.first().end, termination_date)
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
class ChangeProjectsCustomerTest(BaseInvoiceTest):
    def test_delete_invoice_items_if_project_customer_has_been_changed(self):
        self.resource.set_state_ok()
        self.resource.save()
        self.assertEqual(models.Invoice.objects.count(), 1)
        invoice = models.Invoice.objects.first()
        self.assertTrue(invoice.items.filter(scope=self.resource).exists())

        new_customer = structure_factories.CustomerFactory()
        today = timezone.now()
        date = core_utils.month_start(today)
        (
            new_customer_invoice,
            create,
        ) = registrators.RegistrationManager.get_or_create_invoice(new_customer, date)
        self.assertFalse(
            new_customer_invoice.items.filter(scope=self.resource).exists()
        )
        move_project(self.resource.project, new_customer)
        self.assertFalse(invoice.items.filter(scope=self.resource).exists())
        self.assertTrue(new_customer_invoice.items.filter(scope=self.resource).exists())
