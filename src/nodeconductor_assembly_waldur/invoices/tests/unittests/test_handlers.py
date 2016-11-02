from django.test import TestCase

from nodeconductor_assembly_waldur.invoices.tests import factories as invoice_factories
from nodeconductor_assembly_waldur.invoices import models as invoice_models
from nodeconductor_assembly_waldur.packages.tests import fixtures


class InvoiceHandlersTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.PackageFixture()

    def test_existing_invoice_is_updated_on_openstack_package_creation(self):
        invoice = invoice_factories.InvoiceFactory()
        self.fixture.customer = invoice.customer
        package = self.fixture.openstack_package
        self.assertTrue(invoice.openstack_items.filter(package=package).exists())

    def test_new_invoice_is_created_on_openstack_package_creation(self):
        package = self.fixture.openstack_package
        invoice = invoice_models.Invoice.objects.get(customer=package.tenant.service_project_link.project.customer)
        self.assertTrue(invoice.openstack_items.filter(package=package).exists())

    def test_invoice_is_updated_on_openstack_package_deletion(self):
        package = self.fixture.openstack_package
        tenant_name = package.tenant.name
        template_name = package.template.name
        package.delete()
        invoice = invoice_models.Invoice.objects.get(customer=package.tenant.service_project_link.project.customer)
        self.assertTrue(invoice.openstack_items.filter(template_name=template_name, tenant_name=tenant_name).exists())
