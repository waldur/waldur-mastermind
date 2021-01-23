from datetime import timedelta
from unittest import mock

from ddt import data, ddt
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models, tasks, utils
from waldur_mastermind.invoices.tests import factories
from waldur_mastermind.invoices.tests import utils as test_utils
from waldur_mastermind.packages.tests import fixtures as package_fixtures
from waldur_mastermind.packages.tests.utils import override_plugin_settings


@override_plugin_settings(BILLING_ENABLED=True)
class CreateMonthlyInvoicesForPackagesTest(TestCase):
    def test_invoice_is_created_monthly(self):
        with freeze_time('2016-11-01'):
            fixture = package_fixtures.PackageFixture()
            package = fixture.openstack_package

        with freeze_time('2016-12-01'):
            invoice = models.Invoice.objects.get(customer=fixture.customer)

            # Create monthly invoices
            tasks.create_monthly_invoices()

            # Check that old invoices has changed the state
            invoice.refresh_from_db()
            self.assertEqual(invoice.state, models.Invoice.States.CREATED)

            # Check that new invoice where created with the same openstack items
            new_invoice = models.Invoice.objects.get(
                customer=fixture.customer, state=models.Invoice.States.PENDING
            )
            self.assertEqual(package, new_invoice.items.first().scope)

    def test_old_invoices_are_marked_as_created(self):

        # previous year
        with freeze_time('2016-11-01'):
            invoice1 = factories.InvoiceFactory()

        # previous month
        with freeze_time('2017-01-15'):
            invoice2 = factories.InvoiceFactory()

        with freeze_time('2017-02-4'):
            tasks.create_monthly_invoices()
            invoice1.refresh_from_db()
            self.assertEqual(
                invoice1.state,
                models.Invoice.States.CREATED,
                'Invoice for previous year is not marked as CREATED',
            )

            invoice2.refresh_from_db()
            self.assertEqual(
                invoice2.state,
                models.Invoice.States.CREATED,
                'Invoice for previous month is not marked as CREATED',
            )


@ddt
class CheckAccountingStartDateTest(TestCase):
    @data(
        (True, True, True),  # invoice is created if trial period ended
        (True, False, False),  # invoice is not created if trial period has not ended
        (False, True, True),  # invoice is created if trial period is not enabled
    )
    def test_invoice_created_if_trial_period_disabled_or_ended(self, args):
        skip_trial, accounting_started, invoice_exists = args
        if accounting_started:
            accounting_start_date = timezone.now() - timedelta(days=30)
        else:
            accounting_start_date = timezone.now() + timedelta(days=30)

        with override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=skip_trial):
            customer = structure_factories.CustomerFactory()
            customer.accounting_start_date = accounting_start_date
            customer.save()
            tasks.create_monthly_invoices()
            self.assertEqual(
                invoice_exists,
                models.Invoice.objects.filter(customer=customer).exists(),
            )


@override_settings(task_always_eager=True)
@test_utils.override_invoices_settings(
    INVOICE_LINK_TEMPLATE='http://example.com/invoice/{uuid}'
)
class NotificationTest(TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.fixture.owner
        self.invoice = factories.InvoiceFactory(customer=self.fixture.customer)
        self.patcher = mock.patch('waldur_mastermind.invoices.utils.pdfkit')
        mock_pdfkit = self.patcher.start()
        mock_pdfkit.from_string.return_value = b'pdf content'

    def tearDown(self):
        super(NotificationTest, self).tearDown()
        mock.patch.stopall()

    def test_send_invoice_without_pdf(self):
        tasks.send_invoice_notification(self.invoice.uuid)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(len(mail.outbox[0].attachments), 0)

    def test_send_invoice_with_pdf(self):
        utils.create_invoice_pdf(self.invoice)
        tasks.send_invoice_notification(self.invoice.uuid)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(len(mail.outbox[0].attachments), 1)
