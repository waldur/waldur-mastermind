import datetime
from decimal import Decimal
import pytz

from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from nodeconductor.core import utils as core_utils
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
            hours = 24 * ((end - start).days + 1)
            expected_total = hours * package.template.price
            self.assertEqual(invoice.total, expected_total)


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

            hours = 24 * ((utils.get_current_month_end() - timezone.now()).days + 1)
            expected_total = hours * package.template.price
            invoice = models.Invoice.objects.get(customer=package.tenant.service_project_link.project.customer)
            self.assertEqual(invoice.total, expected_total)

    def test_default_tax_percent_is_used_on_invoice_creation(self):
        payment_details = factories.PaymentDetailsFactory(default_tax_percent=20)
        invoice = factories.InvoiceFactory(customer=payment_details.customer)
        self.assertEqual(invoice.tax_percent, payment_details.default_tax_percent)

    def test_package_creation_does_not_increase_price_from_old_package_if_it_is_cheaper(self):
        old_component_price = 100
        new_component_price = old_component_price + 50
        start_date = timezone.datetime(2014, 2, 14, tzinfo=pytz.UTC)
        package_change_date = timezone.datetime(2014, 2, 20, tzinfo=pytz.UTC)

        with freeze_time(start_date):
            old_package = self.create_package(component_price=old_component_price)
        customer = old_package.tenant.service_project_link.project.customer

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = self.create_package_template(single_component_price=new_component_price)
            packages_factories.OpenStackPackageFactory(
                template=new_template,
                tenant__service_project_link__project__customer=customer,
            )

        old_components_price = models.OpenStackItem.calculate_price_for_period(
            price=old_component_price,
            start=start_date,
            end=package_change_date - timezone.timedelta(days=1),
        )

        new_components_price = models.OpenStackItem.calculate_price_for_period(
            price=new_component_price,
            start=package_change_date,
            end=core_utils.month_end(package_change_date),
        )

        expected_price = old_components_price + new_components_price

        # assert
        self.assertEqual(models.Invoice.objects.count(), 1)
        self.assertEqual(Decimal(expected_price), models.Invoice.objects.first().price)

    def test_package_creation_increases_price_from_old_package_if_it_is_more_expensive(self):
        old_component_price = 20
        new_component_price = old_component_price - 10
        start_date = timezone.datetime(2014, 2, 14, tzinfo=pytz.UTC)
        package_change_date = timezone.datetime(2014, 2, 20, tzinfo=pytz.UTC)

        with freeze_time(start_date):
            old_package = self.create_package(component_price=old_component_price)
        customer = old_package.tenant.service_project_link.project.customer

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = self.create_package_template(single_component_price=new_component_price)
            packages_factories.OpenStackPackageFactory(
                template=new_template,
                tenant__service_project_link__project__customer=customer,
            )

        old_components_price = models.OpenStackItem.calculate_price_for_period(
            price=old_component_price,
            start=start_date,
            end=package_change_date,
        )

        new_components_price = models.OpenStackItem.calculate_price_for_period(
            price=new_component_price,
            start=package_change_date + timezone.timedelta(days=1),
            end=core_utils.month_end(package_change_date),
        )

        expected_price = old_components_price + new_components_price

        # assert
        self.assertEqual(models.Invoice.objects.count(), 1)
        self.assertEqual(Decimal(expected_price), models.Invoice.objects.first().price)

    def test_package_creation_does_not_increase_price_from_old_package_if_it_is_cheaper_in_the_end_of_the_month(self):
        old_component_price = 10
        new_component_price = old_component_price + 5
        start_date = timezone.datetime(2014, 2, 20, tzinfo=pytz.UTC)
        package_change_date = timezone.datetime(2014, 2, 28, tzinfo=pytz.UTC)

        with freeze_time(start_date):
            old_package = self.create_package(component_price=old_component_price)
        customer = old_package.tenant.service_project_link.project.customer

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = self.create_package_template(single_component_price=new_component_price)
            packages_factories.OpenStackPackageFactory(
                template=new_template,
                tenant__service_project_link__project__customer=customer,
            )

        old_components_price = models.OpenStackItem.calculate_price_for_period(
            price=old_component_price,
            start=start_date,
            end=package_change_date - timezone.timedelta(days=1),
        )

        new_components_price = models.OpenStackItem.calculate_price_for_period(
            price=new_component_price,
            start=package_change_date,
            end=core_utils.month_end(package_change_date),
        )

        expected_price = old_components_price + new_components_price

        # assert
        self.assertEqual(models.Invoice.objects.count(), 1)
        self.assertEqual(Decimal(expected_price), models.Invoice.objects.first().price)

    def test_package_creation_increases_price_from_old_package_if_it_is_more_expensive_in_the_end_of_the_month(self):
        old_component_price = 15
        new_component_price = old_component_price - 5
        start_date = timezone.datetime(2014, 2, 20, tzinfo=pytz.UTC)
        package_change_date = timezone.datetime(2014, 2, 28, tzinfo=pytz.UTC)

        with freeze_time(start_date):
            old_package = self.create_package(component_price=old_component_price)
        customer = old_package.tenant.service_project_link.project.customer

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = self.create_package_template(single_component_price=new_component_price)
            packages_factories.OpenStackPackageFactory(
                template=new_template,
                tenant__service_project_link__project__customer=customer,
            )

        old_components_price = models.OpenStackItem.calculate_price_for_period(
            price=old_component_price,
            start=start_date,
            end=package_change_date,
        )

        new_components_price = models.OpenStackItem.calculate_price_for_period(
            price=new_component_price,
            start=package_change_date + timezone.timedelta(days=1),
            end=core_utils.month_end(package_change_date + timezone.timedelta(days=1)),
        )

        expected_price = old_components_price + new_components_price

        # assert
        self.assertEqual(models.Invoice.objects.count(), 1)
        self.assertEqual(Decimal(expected_price), models.Invoice.objects.first().price)

    def test_package_creation_does_not_increase_price_for_cheaper_1_day_long_old_package_in_the_end_of_the_month(self):
        old_component_price = 5
        new_component_price = old_component_price + 5
        start_date = timezone.datetime(2014, 2, 27, tzinfo=pytz.UTC)
        package_change_date = timezone.datetime(2014, 2, 28, tzinfo=pytz.UTC)

        with freeze_time(start_date):
            old_package = self.create_package(component_price=old_component_price)
        customer = old_package.tenant.service_project_link.project.customer

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = self.create_package_template(single_component_price=new_component_price)
            packages_factories.OpenStackPackageFactory(
                template=new_template,
                tenant__service_project_link__project__customer=customer,
            )

        old_components_price = models.OpenStackItem.calculate_price_for_period(
            price=old_component_price,
            start=start_date,
            end=package_change_date - timezone.timedelta(days=1),
        )

        new_components_price = models.OpenStackItem.calculate_price_for_period(
            price=new_component_price,
            start=package_change_date,
            end=core_utils.month_end(package_change_date),
        )

        expected_price = old_components_price + new_components_price

        # assert
        self.assertEqual(models.Invoice.objects.count(), 1)
        self.assertEqual(Decimal(expected_price), models.Invoice.objects.first().price)

    def test_package_creation_does_not_increase_price_for_cheaper_1_day_long_old_package_in_the_same_day(self):
        old_component_price = 10
        new_component_price = old_component_price + 5
        start_date = timezone.datetime(2014, 2, 26, tzinfo=pytz.UTC)
        package_change_date = start_date

        with freeze_time(start_date):
            old_package = self.create_package(component_price=old_component_price)
        customer = old_package.tenant.service_project_link.project.customer

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = self.create_package_template(single_component_price=new_component_price)
            packages_factories.OpenStackPackageFactory(
                template=new_template,
                tenant__service_project_link__project__customer=customer,
            )

        old_components_price = models.OpenStackItem.calculate_price_for_period(
            price=old_component_price,
            start=start_date,
            end=package_change_date,
        )

        new_components_price = models.OpenStackItem.calculate_price_for_period(
            price=new_component_price,
            start=package_change_date,
            end=core_utils.month_end(package_change_date),
        )

        expected_price = old_components_price + new_components_price

        # assert
        self.assertEqual(models.Invoice.objects.count(), 1)
        self.assertEqual(Decimal(expected_price), models.Invoice.objects.first().price)

