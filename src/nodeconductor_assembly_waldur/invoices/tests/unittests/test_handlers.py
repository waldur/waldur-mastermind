from django.test import TestCase

from nodeconductor_assembly_waldur.packages.tests import fixtures
from .. import factories
from ... import models


class InvoiceHandlersTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.PackageFixture()

    def test_existing_invoice_is_updated_on_openstack_package_creation(self):
        invoice = factories.InvoiceFactory()
        self.fixture.customer = invoice.customer
        package = self.fixture.openstack_package
        self.assertTrue(invoice.openstack_items.filter(package=package).exists())

    def test_new_invoice_is_created_on_openstack_package_creation(self):
        package = self.fixture.openstack_package
        invoice = models.Invoice.objects.get(customer=package.tenant.service_project_link.project.customer)
        self.assertTrue(invoice.openstack_items.filter(package=package).exists())

    def test_invoice_is_updated_on_openstack_package_deletion(self):
        package = self.fixture.openstack_package
        name = '%s (%s)' % (package.tenant.name, package.template.name)
        package.delete()
        invoice = models.Invoice.objects.get(customer=package.tenant.service_project_link.project.customer)
        self.assertEqual(invoice.openstack_items.first().name, name)
