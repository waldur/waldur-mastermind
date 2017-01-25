import datetime

from django.db.models.signals import pre_delete
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time
from mock import Mock

from nodeconductor_assembly_waldur.packages.models import OpenStackPackage
from nodeconductor_assembly_waldur.packages.tests import factories as packages_factories

from .. import factories, fixtures
from ... import models, utils


class UpdateInvoiceOnOpenstackPackageDeletionTest(TestCase):

    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def test_invoice_item_name_is_saved_on_package_deletion(self):
        package = self.fixture.openstack_package

        package.delete()

        invoice = models.Invoice.objects.get(customer=package.tenant.service_project_link.project.customer)
        expected_name = '%s (%s)' % (package.tenant.name, package.template.name)
        item = invoice.openstack_items.first()
        self.assertEqual(item.name, expected_name)

    def test_invoice_price_changes_on_package_deletion(self):
        start = datetime.datetime(year=2016, month=11, day=4, hour=12, minute=0, second=0)
        end = datetime.datetime(year=2016, month=11, day=25, hour=18, minute=0, second=0)

        with freeze_time(start):
            package = self.fixture.openstack_package

        with freeze_time(end):
            package.delete()

            invoice = models.Invoice.objects.get(customer=package.tenant.service_project_link.project.customer)
            days = (end - start).days + 1
            expected_total = days * package.template.price
            self.assertEqual(invoice.total, expected_total)

    def test_invoice_update_handler_is_called_once_on_tenant_deletion(self):
        mocked_handler = Mock()
        pre_delete.connect(mocked_handler, sender=OpenStackPackage, dispatch_uid='test_handler')

        package = self.fixture.openstack_package
        package.tenant.delete()
        self.assertEqual(mocked_handler.call_count, 1)


class AddNewOpenstackPackageDetailsToInvoiceTest(TestCase):

    def create_package_template(self, single_component_price=10, components_amount=1):
        template = packages_factories.PackageTemplateFactory()
        first_component = template.components.first()
        first_component.price = single_component_price
        first_component.amount = components_amount
        first_component.save()
        return template

    def create_package(self, component_price):
        template = self.create_package_template(single_component_price=component_price)
        package = packages_factories.OpenStackPackageFactory(template=template)
        return package

    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def test_existing_invoice_is_updated_on_openstack_package_creation(self):
        invoice = factories.InvoiceFactory()
        self.fixture.customer = invoice.customer
        package = self.fixture.openstack_package
        self.assertTrue(invoice.openstack_items.filter(package=package).exists())

    def test_new_invoice_is_created_on_openstack_package_creation(self):
        package = self.fixture.openstack_package
        invoice = models.Invoice.objects.get(customer=package.tenant.service_project_link.project.customer)
        self.assertTrue(invoice.openstack_items.filter(package=package).exists())

    def test_invoice_price_is_calculated_on_package_creation(self):
        with freeze_time('2016-11-04 12:00:00'):
            package = self.fixture.openstack_package

            days = (utils.get_current_month_end() - timezone.now()).days + 1
            expected_total = days * package.template.price

        with freeze_time(utils.get_current_month_end()):
            invoice = models.Invoice.objects.get(customer=package.tenant.service_project_link.project.customer)
            self.assertEqual(invoice.total, expected_total)

    def test_default_tax_percent_is_used_on_invoice_creation(self):
        payment_details = factories.PaymentDetailsFactory(default_tax_percent=20)
        invoice = factories.InvoiceFactory(customer=payment_details.customer)
        self.assertEqual(invoice.tax_percent, payment_details.default_tax_percent)
