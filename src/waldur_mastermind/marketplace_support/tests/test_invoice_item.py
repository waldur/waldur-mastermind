import decimal
from calendar import monthrange
from decimal import Decimal

import pytz
from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase
from django.utils import timezone
from freezegun.api import freeze_time

from waldur_core.core import utils as core_utils
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices import models, registrators, utils
from waldur_mastermind.invoices.tests import factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support.tests import fixtures as support_fixtures


class BaseSupportInvoiceTest(TransactionTestCase):
    def setUp(self):
        self.fixture = support_fixtures.SupportFixture()
        self.offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering
        )
        self.plan = marketplace_factories.PlanFactory(
            unit=marketplace_models.Plan.Units.PER_MONTH
        )
        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.plan, component=offering_component, price=7,
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering, project=self.fixture.project, plan=self.plan,
        )

    def get_factor(self, start_date, usage_days):
        month_days = monthrange(start_date.year, start_date.month)[1]
        return quantize_price(decimal.Decimal(usage_days) / month_days)


class ResourceCreationInvoiceTest(BaseSupportInvoiceTest):
    def test_invoice_is_created_on_resource_creation(self):
        self.resource.set_state_ok()
        self.resource.save()
        self.assertEqual(models.Invoice.objects.count(), 1)
        invoice = models.Invoice.objects.first()
        self.assertTrue(invoice.items.filter(scope=self.resource).exists())

    def test_invoice_is_not_created_for_pending_resource(self):
        pending_resource = marketplace_factories.ResourceFactory(
            offering=self.offering, project=self.fixture.project, plan=self.plan,
        )

        self.resource.set_state_ok()
        self.resource.save()

        self.assertEqual(models.Invoice.objects.count(), 1)
        invoice = models.Invoice.objects.first()
        self.assertTrue(invoice.items.filter(scope=self.resource).exists())
        self.assertFalse(invoice.items.filter(scope=pending_resource).exists())

    def test_existing_invoice_is_updated_on_resource_creation(self):
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


class ResourceDeletionInvoiceTest(BaseSupportInvoiceTest):
    def test_invoice_price_is_not_changed_after_a_while_if_resource_is_deleted(self):
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
            self.assertEqual(models.Invoice.objects.count(), 1)
            self.assertEqual(self.get_invoice_items(invoice).count(), 1)

        with freeze_time(next_month):
            new_invoice, _ = registrators.RegistrationManager.get_or_create_invoice(
                self.resource.project.customer, next_month
            )
            self.resource.set_state_terminating()
            self.resource.save()
            self.resource.set_state_terminated()
            self.resource.save()
            self.assertEqual(self.get_invoice_items(new_invoice).count(), 1)
            self.assertEqual(
                self.get_invoice_items(new_invoice).first().end, next_month
            )

    def get_invoice_items(self, invoice):
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


class ResourceStateChangeInvoiceTest(BaseSupportInvoiceTest):
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
