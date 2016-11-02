import datetime

from django.test import TestCase

from nodeconductor_assembly_waldur.packages.tests import fixtures as package_fixtures

from nodeconductor_assembly_waldur.invoices import models, tasks


class InvoiceTasksTest(TestCase):
    def test_invoice_is_created_monthly(self):
        fixture = package_fixtures.PackageFixture()
        fixture.openstack_package
        today = datetime.date.today()
        # Make invoice expired
        invoice = models.Invoice.objects.get(customer=fixture.customer)
        invoice.month = today.month - 1 if today.month != 1 else 12
        invoice.year = today.year if today.month != 1 else today.year - 1
        invoice.save(update_fields=['year', 'month'])

        # Create monthly invoices
        tasks.create_monthly_invoices_for_openstack_packages()

        # Check that old invoices changed the state
        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.BILLED)

        # Check that new invoice where created with the same openstack items
        new_invoice = models.Invoice.objects.get(customer=fixture.customer, state=models.Invoice.States.PENDING)
        self.assertEqual(invoice.openstack_items.first().tenant_name, new_invoice.openstack_items.first().tenant_name)
