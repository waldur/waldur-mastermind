from datetime import timedelta

from ddt import ddt, data
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from nodeconductor.structure.tests import factories as structure_factories
from nodeconductor_assembly_waldur.invoices.tests.utils import override_invoices_settings
from nodeconductor_assembly_waldur.packages.tests import fixtures as package_fixtures

from .. import factories
from ... import models, tasks


class CreateMonthlyInvoicesForPackagesTest(TestCase):

    def test_invoice_is_created_monthly(self):
        with freeze_time('2016-11-01 00:00:00'):
            fixture = package_fixtures.PackageFixture()
            package = fixture.openstack_package

        with freeze_time('2016-12-01 00:00:00'):
            invoice = models.Invoice.objects.get(customer=fixture.customer)

            # Create monthly invoices
            tasks.create_monthly_invoices()

            # Check that old invoices has changed the state
            invoice.refresh_from_db()
            self.assertEqual(invoice.state, models.Invoice.States.CREATED)

            # Check that new invoice where created with the same openstack items
            new_invoice = models.Invoice.objects.get(customer=fixture.customer, state=models.Invoice.States.PENDING)
            self.assertEqual(package, new_invoice.openstack_items.first().package)

    def test_old_invoices_are_marked_as_created(self):

        # previous year
        with freeze_time('2016-11-01 00:00:00'):
            invoice1 = factories.InvoiceFactory()

        # previous month
        with freeze_time('2017-01-15 00:00:00'):
            invoice2 = factories.InvoiceFactory()

        with freeze_time('2017-02-4 00:00:00'):
            tasks.create_monthly_invoices()
            invoice1.refresh_from_db()
            self.assertEqual(invoice1.state, models.Invoice.States.CREATED,
                             'Invoice for previous year is not marked as CREATED')

            invoice2.refresh_from_db()
            self.assertEqual(invoice2.state, models.Invoice.States.CREATED,
                             'Invoice for previous month is not marked as CREATED')


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

        with override_invoices_settings(ENABLE_ACCOUNTING_START_DATE=skip_trial):
            customer = structure_factories.CustomerFactory()
            models.PaymentDetails.objects.create(customer=customer, accounting_start_date=accounting_start_date)
            tasks.create_monthly_invoices()
            self.assertEqual(invoice_exists, models.Invoice.objects.filter(customer=customer).exists())
