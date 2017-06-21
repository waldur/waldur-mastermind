from django.test import TestCase

from nodeconductor_assembly_waldur.invoices.tasks import format_invoice_csv

from .. import models
from . import fixtures


class TestReportFormatter(TestCase):
    def test_invoice_items_are_properly_formatted(self):
        fixture = fixtures.InvoiceFixture()
        package = fixture.openstack_package
        invoice = models.Invoice.objects.get(customer=package.tenant.service_project_link.project.customer)
        report = format_invoice_csv(invoice)
        self.assertEqual(2, len(report.splitlines()))
