from django.test import TestCase
from freezegun import freeze_time

from nodeconductor_assembly_waldur.packages.tests import fixtures as package_fixtures

from ... import models, tasks


class InvoiceTasksTest(TestCase):
    def test_invoice_is_created_monthly(self):
        with freeze_time('2016-11-01 00:00:00'):
            fixture = package_fixtures.PackageFixture()
            package = fixture.openstack_package

        with freeze_time('2016-12-01 00:00:00'):
            # Make invoice expired
            invoice = models.Invoice.objects.get(customer=fixture.customer)

            # Create monthly invoices
            tasks.create_monthly_invoices_for_packages()

            # Check that old invoices has changed the state
            invoice.refresh_from_db()
            self.assertEqual(invoice.state, models.Invoice.States.BILLED)

            # Check that new invoice where created with the same openstack items
            new_invoice = models.Invoice.objects.get(customer=fixture.customer, state=models.Invoice.States.PENDING)
            self.assertEqual(package, new_invoice.openstack_items.first().package)
