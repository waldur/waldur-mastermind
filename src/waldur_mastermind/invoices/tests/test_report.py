import datetime
from unittest import mock

from django.test import TransactionTestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_mastermind.invoices.tasks import format_invoice_csv
from waldur_mastermind.packages.tests.utils import override_plugin_settings
from waldur_mastermind.support.tests import factories as support_factories

from .. import models, tasks
from . import fixtures, utils


@override_plugin_settings(BILLING_ENABLED=True)
class BaseReportFormatterTest(TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        package = fixtures.create_package(100, self.fixture.openstack_tenant)
        package.template.name = 'New package template'
        package.template.save()
        self.customer = package.tenant.service_project_link.project.customer

        invoice = models.Invoice.objects.get(customer=self.customer)
        invoice.customer.agreement_number = 100
        invoice.customer.save()
        invoice.set_created()
        self.invoice = invoice


class GenericReportFormatterTest(BaseReportFormatterTest):
    def test_invoice_items_are_properly_formatted(self):
        report = format_invoice_csv(self.invoice)
        lines = report.splitlines()
        self.assertEqual(2, len(lines))

        expected_header = (
            'customer_uuid;customer_name;project_uuid;project_name;'
            'invoice_uuid;invoice_number;invoice_year;invoice_month;'
            'invoice_date;due_date;invoice_price;invoice_tax;'
            'invoice_total;name;article_code;product_code;'
            'price;tax;total;unit_price;unit;start;end'
        )
        self.assertEqual(lines[0], expected_header)

    def test_offering_items_are_serialized(self):
        offering = support_factories.OfferingFactory(
            template__name='OFFERING-001', project=self.fixture.project
        )
        offering.state = offering.__class__.States.OK
        offering.save()
        report = format_invoice_csv(self.invoice)
        lines = report.splitlines()
        self.assertEqual(3, len(lines))
        self.assertTrue('OFFERING-001' in ''.join(lines))


INVOICE_REPORTING = {
    'ENABLE': True,
    'USE_SAF': True,
    'SAF_PARAMS': {'RMAKSULIPP': '20%', 'ARTPROJEKT': 'PROJEKT',},
    'CSV_PARAMS': {'delimiter': str(';'),},
    'EMAIL': 'test@example.com',
}


@utils.override_invoices_settings(INVOICE_REPORTING=INVOICE_REPORTING)
@freeze_time('2017-09-26')
class SafReportFormatterTest(BaseReportFormatterTest):
    def test_invoice_items_are_properly_formatted(self):
        report = format_invoice_csv(self.invoice)
        lines = report.splitlines()
        self.assertEqual(2, len(lines))

        expected_header = (
            'DOKNR;KUUPAEV;VORMKUUP;MAKSEAEG;YKSUS;PARTNER;'
            'ARTIKKEL;KOGUS;SUMMA;RMAKSUSUM;RMAKSULIPP;'
            'ARTPROJEKT;ARTNIMI;VALI;U_KONEDEARV;H_PERIOOD'
        )
        self.assertEqual(lines[0], expected_header)

        expected_data = (
            '{};30.09.2017;26.09.2017;26.10.2017;100;100;;5;1500.00;0.00;20%;'
            'PROJEKT;{} (Small / PackageTemplate);Record no {};;01.09.2017-30.09.2017'.format(
                self.invoice.number, self.fixture.openstack_tenant, self.invoice.number
            )
        )
        self.assertEqual(lines[1], expected_data)

    def test_partner_number_is_overriden_if_customer_sponsor_number_is_set(self):
        self.invoice.customer.sponsor_number = 99999999
        self.invoice.customer.save()

        report = format_invoice_csv(self.invoice)
        self.assertTrue('99999999' in report)

    def test_usage_based_item_is_skipped_if_quantity_is_zero(self):
        item = self.invoice.items.first()
        item.unit = models.InvoiceItem.Units.QUANTITY
        item.quantity = 0
        item.save()

        report = format_invoice_csv(self.invoice)
        lines = report.splitlines()[1:]
        self.assertEqual(0, len(lines))

    def test_usage_based_item_is_skipped_if_unit_price_is_zero(self):
        item = self.invoice.items.first()
        item.unit = models.InvoiceItem.Units.QUANTITY
        item.quantity = 10
        item.unit_price = 0
        item.save()

        report = format_invoice_csv(self.invoice)
        lines = report.splitlines()[1:]
        self.assertEqual(0, len(lines))


@freeze_time('2017-11-01')
@mock.patch('waldur_mastermind.invoices.tasks.core_utils.send_mail_with_attachment')
class InvoiceReportTaskTest(BaseReportFormatterTest):
    def setUp(self):
        super(InvoiceReportTaskTest, self).setUp()
        self.invoice.year = 2017
        self.invoice.month = 10
        self.invoice.save()

    @utils.override_invoices_settings(INVOICE_REPORTING=INVOICE_REPORTING)
    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_demo_customers_are_skipped_if_accounting_start_is_enabled(
        self, send_mail_mock
    ):
        self.customer.accounting_start_date = timezone.now() + datetime.timedelta(
            days=10
        )
        self.customer.save()
        tasks.send_invoice_report()
        message = send_mail_mock.call_args[1]['attachment']
        lines = message.splitlines()[1:]
        self.assertEqual(0, len(lines))

    @utils.override_invoices_settings(INVOICE_REPORTING=INVOICE_REPORTING)
    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=False)
    def test_demo_customers_are_not_skipped_if_accounting_start_is_not_enabled(
        self, send_mail_mock
    ):
        self.customer.accounting_start_date = timezone.now() + datetime.timedelta(
            days=10
        )
        self.customer.save()
        tasks.send_invoice_report()
        message = send_mail_mock.call_args[1]['attachment']
        lines = message.splitlines()
        self.assertEqual(2, len(lines))

    @utils.override_invoices_settings(INVOICE_REPORTING=INVOICE_REPORTING)
    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_active_customers_are_not_skipped_anyways(self, send_mail_mock):
        self.customer.accounting_start_date = timezone.now() - datetime.timedelta(
            days=50
        )
        self.customer.save()
        tasks.send_invoice_report()
        message = send_mail_mock.call_args[1]['attachment']
        lines = message.splitlines()
        self.assertEqual(2, len(lines))

    @utils.override_invoices_settings(INVOICE_REPORTING=INVOICE_REPORTING)
    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_active_customer_is_not_skipped_if_it_has_been_actived_in_previous_month(
        self, send_mail_mock
    ):
        self.customer.accounting_start_date = timezone.now() - datetime.timedelta(
            days=15
        )
        self.customer.save()
        tasks.send_invoice_report()
        message = send_mail_mock.call_args[1]['attachment']
        lines = message.splitlines()
        self.assertEqual(2, len(lines))

    @utils.override_invoices_settings(INVOICE_REPORTING=INVOICE_REPORTING)
    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_empty_invoice_is_skipped(self, send_mail_mock):
        self.customer.accounting_start_date = timezone.now() - datetime.timedelta(
            days=50
        )
        self.customer.save()
        for item in self.invoice.items:
            item.delete()

        tasks.send_invoice_report()
        message = send_mail_mock.call_args[1]['attachment']
        lines = message.splitlines()[1:]
        self.assertEqual(0, len(lines))

    @utils.override_invoices_settings(INVOICE_REPORTING=INVOICE_REPORTING)
    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_empty_invoice_item_is_skipped(self, send_mail_mock):
        self.customer.accounting_start_date = timezone.now() - datetime.timedelta(
            days=50
        )
        self.customer.save()
        for item in self.invoice.items:
            item.unit_price = 0
            item.save()

        tasks.send_invoice_report()
        message = send_mail_mock.call_args[1]['attachment']
        lines = message.splitlines()[1:]
        self.assertEqual(0, len(lines))

    @utils.override_invoices_settings(INVOICE_REPORTING=INVOICE_REPORTING)
    def test_active_invoice_are_merged(self, send_mail_mock):
        self.customer.accounting_start_date = timezone.now() - datetime.timedelta(
            days=50
        )
        self.customer.save()
        fixture = fixtures.InvoiceFixture()
        package = fixtures.create_package(111, fixture.openstack_tenant)
        package.template.name = 'PackageTemplate'
        package.template.save()

        invoice = models.Invoice.objects.get(
            customer=package.tenant.service_project_link.project.customer
        )
        invoice.customer.agreement_number = 777
        invoice.customer.save()
        invoice.year = 2017
        invoice.month = 10
        invoice.save()
        invoice.set_created()

        customer = package.tenant.service_project_link.project.customer
        customer.accounting_start_date = timezone.now() - datetime.timedelta(days=50)
        customer.save()

        tasks.send_invoice_report()
        message = send_mail_mock.call_args[1]['attachment']
        lines = message.splitlines()
        self.assertEqual(3, len(lines))
