import datetime
from unittest import mock

from django.test import TransactionTestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_mastermind.invoices import models, tasks
from waldur_mastermind.invoices import utils as invoices_utils
from waldur_mastermind.invoices.tasks import format_invoice_csv
from waldur_mastermind.invoices.tests import factories, fixtures, utils


class BaseReportFormatterTest(TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

        self.customer = self.fixture.customer
        self.customer.agreement_number = 100
        self.customer.contact_details = 'Contact details'
        self.customer.save()

        self.invoice = self.fixture.invoice
        self.invoice.set_created()
        self.invoice_item = self.fixture.invoice_item


class GenericReportFormatterTest(BaseReportFormatterTest):
    def test_invoice_items_are_properly_formatted(self):
        report = format_invoice_csv(self.invoice)
        lines = report.splitlines()
        self.assertEqual(2, len(lines))

        expected_header = (
            'customer_uuid;customer_name;project_uuid;project_name;'
            'invoice_uuid;invoice_number;invoice_year;invoice_month;'
            'invoice_date;due_date;invoice_price;invoice_tax;'
            'invoice_total;name;article_code;'
            'price;tax;total;unit_price;unit;start;end'
        )
        self.assertEqual(lines[0], expected_header)
        self.assertTrue('OFFERING-001' in lines[1])


INVOICE_REPORTING = {
    'ENABLE': True,
    'USE_SAF': True,
    'SAF_PARAMS': {
        'RMAKSULIPP': '20%',
        'ARTPROJEKT': 'PROJEKT',
    },
    'CSV_PARAMS': {
        'delimiter': str(';'),
    },
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
            'ARTPROJEKT;ARTNIMI;VALI;U_KONEDEARV;U_GRUPPITUNNUS;H_PERIOOD'
        )
        self.assertEqual(lines[0], expected_header)

        expected_data = (
            '{};30.09.2017;26.09.2017;26.10.2017;100;100;;30;300.00;0.00;20%;'
            'PROJEKT;OFFERING-001;Record no {}. Contact details;;{};01.09.2017-30.09.2017'
        ).format(
            self.invoice.number, self.invoice.number, self.invoice_item.project_name
        )
        self.assertEqual(lines[1], expected_data)

    def test_partner_number_is_overridden_if_customer_sponsor_number_is_set(self):
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
@utils.override_invoices_settings(INVOICE_REPORTING=INVOICE_REPORTING)
class InvoiceReportTaskTest(BaseReportFormatterTest):
    def setUp(self):
        super(InvoiceReportTaskTest, self).setUp()
        self.invoice.year = 2017
        self.invoice.month = 10
        self.invoice.save()

    def send_report(self):
        with mock.patch(
            'waldur_mastermind.invoices.tasks.core_utils.send_mail'
        ) as send_mail_mock:
            tasks.send_invoice_report()
            message = send_mail_mock.call_args[1]['attachment']
            # first line is header
            lines = message.splitlines()[1:]
            return lines

    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_demo_customers_are_skipped_if_accounting_start_is_enabled(
        self,
    ):
        self.customer.accounting_start_date = timezone.now() + datetime.timedelta(
            days=10
        )
        self.customer.save()
        lines = self.send_report()
        self.assertEqual(0, len(lines))

    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=False)
    def test_demo_customers_are_not_skipped_if_accounting_start_is_not_enabled(self):
        self.customer.accounting_start_date = timezone.now() + datetime.timedelta(
            days=10
        )
        self.customer.save()
        lines = self.send_report()
        self.assertEqual(1, len(lines))

    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_active_customers_are_not_skipped_anyways(self):
        self.customer.accounting_start_date = timezone.now() - datetime.timedelta(
            days=50
        )
        self.customer.save()
        lines = self.send_report()
        self.assertEqual(1, len(lines))

    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_active_customer_is_not_skipped_if_it_has_been_activated_in_previous_month(
        self,
    ):
        self.customer.accounting_start_date = timezone.now() - datetime.timedelta(
            days=15
        )
        self.customer.save()
        lines = self.send_report()
        self.assertEqual(1, len(lines))

    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_invoice_without_items_is_skipped(self):
        self.customer.accounting_start_date = timezone.now() - datetime.timedelta(
            days=50
        )
        self.customer.save()
        for item in self.invoice.items.all():
            item.delete()

        lines = self.send_report()
        self.assertEqual(0, len(lines))

    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_invoice_with_all_zero_priced_items_is_skipped(self):
        self.customer.accounting_start_date = timezone.now() - datetime.timedelta(
            days=50
        )
        self.customer.save()
        for item in self.invoice.items.all():
            item.unit_price = 0
            item.save()

        lines = self.send_report()
        self.assertEqual(0, len(lines))

    def test_all_active_organizations_are_rendered_in_one_invoice(self):
        # First customer is active and it his invoice has one item
        self.customer.accounting_start_date = timezone.now() - datetime.timedelta(
            days=50
        )
        self.customer.save()

        # Second customer is active and his invoice has one item
        fixture = fixtures.InvoiceFixture()
        invoice = fixture.invoice
        invoice.customer.accounting_start_date = timezone.now() - datetime.timedelta(
            days=50
        )
        invoice.customer.save()
        invoice.year = 2017
        invoice.month = 10
        invoice.save()
        fixture.invoice_item

        lines = self.send_report()
        # Report has invoice items for both organizations
        self.assertEqual(2, len(lines))


@freeze_time('2017-11-01')
class MonthlyReportTaskTest(TransactionTestCase):
    @utils.override_invoices_settings(INVOICE_REPORTING=True)
    def test_report_context(self):
        invoice_1 = factories.InvoiceFactory()
        invoice_2 = factories.InvoiceFactory()
        invoice_3 = factories.InvoiceFactory()
        factories.PaymentProfileFactory(
            organization=invoice_1.customer,
            payment_type=models.PaymentType.FIXED_PRICE,
            is_active=True,
            attributes={'end_date': '2017-10-01', 'contract_sum': 100},
        )
        factories.PaymentProfileFactory(
            organization=invoice_2.customer,
            payment_type=models.PaymentType.FIXED_PRICE,
            is_active=True,
        )
        factories.PaymentProfileFactory(
            organization=invoice_2.customer,
            payment_type=models.PaymentType.FIXED_PRICE,
            is_active=False,
        )
        context = invoices_utils.get_monthly_invoicing_reports_context()
        self.assertEqual(len(context['invoices']), 1)
        self.assertEqual(context['invoices'][0], invoice_3)
        self.assertEqual(len(context['contracts']), 2)
        customer_1_context = [
            c
            for c in context['contracts']
            if c['name'] == invoice_1.customer.abbreviation
        ][0]
        self.assertEqual(customer_1_context['end_date_alarm'], True)
        self.assertEqual(customer_1_context['payments_alarm'], True)
        customer_2_context = [
            c
            for c in context['contracts']
            if c['name'] == invoice_2.customer.abbreviation
        ][0]
        self.assertEqual(customer_2_context['end_date_alarm'], False)
        self.assertEqual(customer_2_context['payments_alarm'], None)
