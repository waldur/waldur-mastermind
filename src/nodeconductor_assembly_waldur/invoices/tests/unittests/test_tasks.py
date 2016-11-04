from datetime import date
from dateutil.relativedelta import relativedelta

from django.test import TestCase

from nodeconductor_assembly_waldur.packages.tests import fixtures as package_fixtures

from nodeconductor_assembly_waldur.invoices import models, tasks


class InvoiceTasksTest(TestCase):
    def test_invoice_is_created_monthly(self):
        fixture = package_fixtures.PackageFixture()
        fixture.openstack_package

        # Make invoice expired
        invoice = models.Invoice.objects.get(customer=fixture.customer)
        month_ago = date.today() - relativedelta(months=1)
        invoice.month = month_ago.month
        invoice.year = month_ago.year
        invoice.save(update_fields=['year', 'month'])

        # Create monthly invoices
        tasks.create_monthly_invoices_for_openstack_packages()

        # Check that old invoices has changed the state
        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.BILLED)

        # Check that new invoice where created with the same openstack items
        new_invoice = models.Invoice.objects.get(customer=fixture.customer, state=models.Invoice.States.PENDING)
        self.assertEqual(invoice.openstack_items.first().name, new_invoice.openstack_items.first().name)
