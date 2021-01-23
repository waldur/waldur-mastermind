import datetime
from decimal import Decimal
from unittest.mock import Mock

import pytz
from django.db.models.signals import pre_delete
from django.test import TransactionTestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import utils as invoices_utils
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.invoices.tests import fixtures as invoices_fixtures
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages.tests import factories as packages_factories
from waldur_mastermind.packages.tests.utils import override_plugin_settings


@override_plugin_settings(BILLING_ENABLED=True)
class UpdateInvoiceOnOpenstackPackageDeletionTest(TransactionTestCase):
    def setUp(self):
        self.fixture = invoices_fixtures.InvoiceFixture()

    def test_invoice_item_name_is_saved_on_package_deletion(self):
        package = self.fixture.openstack_package

        package.delete()

        invoice = invoices_models.Invoice.objects.get(
            customer=package.tenant.service_project_link.project.customer
        )
        expected_name = '%s (%s / %s)' % (
            package.tenant.name,
            package.template.get_category_display(),
            package.template.name,
        )
        item = invoice.items.first()
        self.assertEqual(item.name, expected_name)

    def test_invoice_price_changes_on_package_deletion(self):
        start = datetime.datetime(
            year=2016, month=11, day=4, hour=12, minute=0, second=0
        )
        end = datetime.datetime(
            year=2016, month=11, day=25, hour=18, minute=0, second=0
        )

        with freeze_time(start):
            package = self.fixture.openstack_package

        with freeze_time(end):
            package.delete()

            invoice = invoices_models.Invoice.objects.get(
                customer=package.tenant.service_project_link.project.customer
            )
            days = (end - start).days + 1
            expected_total = days * package.template.price
            self.assertEqual(invoice.total, expected_total)

    def test_invoice_update_handler_is_called_once_on_tenant_deletion(self):
        mocked_handler = Mock()
        pre_delete.connect(
            mocked_handler,
            sender=package_models.OpenStackPackage,
            dispatch_uid='test_handler',
        )

        package = self.fixture.openstack_package
        package.tenant.delete()
        self.assertEqual(mocked_handler.call_count, 1)


@override_plugin_settings(BILLING_ENABLED=True)
class AddNewOpenstackPackageDetailsToInvoiceTest(TransactionTestCase):
    def setUp(self):
        self.fixture = invoices_fixtures.InvoiceFixture()

    def test_existing_invoice_is_updated_on_openstack_package_creation(self):
        self.fixture.customer = self.fixture.invoice.customer
        package = self.fixture.openstack_package
        self.assertTrue(self.fixture.invoice.items.filter(scope=package).exists())

    def test_if_provisioning_failed_invoice_item_is_not_created(self):
        self.fixture.customer = self.fixture.invoice.customer
        self.fixture.openstack_tenant.backend_id = None
        self.fixture.openstack_tenant.save()
        package = self.fixture.openstack_package
        self.assertFalse(self.fixture.invoice.items.filter(scope=package).exists())

    def test_if_provisioning_succeeded_invoice_item_is_created(self):
        self.fixture.customer = self.fixture.invoice.customer
        tenant = self.fixture.openstack_tenant
        tenant.backend_id = None
        tenant.save()
        package = self.fixture.openstack_package
        tenant.backend_id = 'VALID_ID'
        tenant.save()
        self.assertTrue(self.fixture.invoice.items.filter(scope=package).exists())

    def test_new_invoice_is_created_on_openstack_package_creation(self):
        package = self.fixture.openstack_package
        invoice = invoices_models.Invoice.objects.get(
            customer=package.tenant.service_project_link.project.customer
        )
        self.assertTrue(invoice.items.filter(scope=package).exists())

    def test_invoice_price_is_calculated_on_package_creation(self):
        with freeze_time('2016-11-04 12:00:00'):
            package = self.fixture.openstack_package

            days = (invoices_utils.get_current_month_end() - timezone.now()).days + 1
            expected_total = days * package.template.price

        with freeze_time(invoices_utils.get_current_month_end()):
            invoice = invoices_models.Invoice.objects.get(
                customer=package.tenant.service_project_link.project.customer
            )
            self.assertEqual(invoice.total, expected_total)

    def test_default_tax_percent_is_used_on_invoice_creation(self):
        customer = structure_factories.CustomerFactory()
        customer.default_tax_percent = 20
        customer.save()
        invoice = invoices_factories.InvoiceFactory(customer=customer)
        self.assertEqual(invoice.tax_percent, customer.default_tax_percent)

    def test_package_creation_does_not_increase_price_from_old_package_if_it_is_cheaper(
        self,
    ):
        old_component_price = 100
        new_component_price = old_component_price + 50
        start_date = timezone.datetime(2014, 2, 14, tzinfo=pytz.UTC)
        package_change_date = timezone.datetime(2014, 2, 20, tzinfo=pytz.UTC)
        end_of_the_month = core_utils.month_end(package_change_date)

        with freeze_time(start_date):
            old_package = invoices_fixtures.create_package(
                component_price=old_component_price
            )
        customer = old_package.tenant.service_project_link.project.customer

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = invoices_fixtures.create_package_template(
                component_price=new_component_price
            )
            new_package = packages_factories.OpenStackPackageFactory(
                template=new_template,
                tenant__service_project_link__project__customer=customer,
            )

        old_components_price = (
            old_package.template.price * (package_change_date - start_date).days
        )
        second_component_usage_days = invoices_utils.get_full_days(
            package_change_date, end_of_the_month
        )
        new_components_price = new_package.template.price * second_component_usage_days
        expected_price = old_components_price + new_components_price

        self.assertEqual(invoices_models.Invoice.objects.count(), 1)
        self.assertEqual(
            Decimal(expected_price), invoices_models.Invoice.objects.first().price
        )

    def test_package_creation_increases_price_from_old_package_if_it_is_more_expensive(
        self,
    ):
        old_component_price = 20
        new_component_price = old_component_price - 10
        start_date = timezone.datetime(2014, 2, 14, tzinfo=pytz.UTC)
        package_change_date = timezone.datetime(2014, 2, 20, tzinfo=pytz.UTC)
        end_of_the_month = core_utils.month_end(package_change_date)

        with freeze_time(start_date):
            old_package = invoices_fixtures.create_package(
                component_price=old_component_price
            )
        tenant = old_package.tenant

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = invoices_fixtures.create_package_template(
                component_price=new_component_price
            )
            new_package = packages_factories.OpenStackPackageFactory(
                template=new_template, tenant=tenant,
            )

        old_components_price = old_package.template.price * (
            (package_change_date - start_date).days + 1
        )
        second_component_usage_days = (
            invoices_utils.get_full_days(package_change_date, end_of_the_month) - 1
        )
        new_components_price = new_package.template.price * second_component_usage_days
        expected_price = old_components_price + new_components_price

        # assert
        self.assertEqual(invoices_models.Invoice.objects.count(), 1)
        self.assertEqual(
            Decimal(expected_price), invoices_models.Invoice.objects.first().price
        )

    def test_package_creation_does_not_increase_price_from_old_package_if_it_is_cheaper_in_the_end_of_the_month(
        self,
    ):
        old_component_price = 10
        new_component_price = old_component_price + 5
        start_date = timezone.datetime(2014, 2, 20, tzinfo=pytz.UTC)
        package_change_date = timezone.datetime(2014, 2, 28, tzinfo=pytz.UTC)
        end_of_the_month = core_utils.month_end(package_change_date)

        with freeze_time(start_date):
            old_package = invoices_fixtures.create_package(
                component_price=old_component_price
            )
        tenant = old_package.tenant

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = invoices_fixtures.create_package_template(
                component_price=new_component_price
            )
            new_package = packages_factories.OpenStackPackageFactory(
                template=new_template, tenant=tenant
            )

        old_components_price = (
            old_package.template.price * (package_change_date - start_date).days
        )
        second_component_usage_days = invoices_utils.get_full_days(
            package_change_date, end_of_the_month
        )
        new_components_price = new_package.template.price * second_component_usage_days
        expected_price = old_components_price + new_components_price

        # assert
        self.assertEqual(invoices_models.Invoice.objects.count(), 1)
        self.assertEqual(
            Decimal(expected_price), invoices_models.Invoice.objects.first().price
        )

    def test_package_creation_increases_price_from_old_package_if_it_is_more_expensive_in_the_end_of_the_month(
        self,
    ):
        old_component_price = 15
        new_component_price = old_component_price - 5
        start_date = timezone.datetime(2014, 2, 20, tzinfo=pytz.UTC)
        package_change_date = timezone.datetime(2014, 2, 27, tzinfo=pytz.UTC)
        end_of_the_month = core_utils.month_end(package_change_date)

        with freeze_time(start_date):
            old_package = invoices_fixtures.create_package(
                component_price=old_component_price
            )
        tenant = old_package.tenant

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = invoices_fixtures.create_package_template(
                component_price=new_component_price
            )
            new_package = packages_factories.OpenStackPackageFactory(
                template=new_template, tenant=tenant,
            )

        old_components_price = old_package.template.price * (
            (package_change_date - start_date).days + 1
        )
        second_component_usage_days = (
            invoices_utils.get_full_days(package_change_date, end_of_the_month) - 1
        )
        new_components_price = new_package.template.price * second_component_usage_days
        expected_price = old_components_price + new_components_price

        # assert
        self.assertEqual(invoices_models.Invoice.objects.count(), 1)
        self.assertEqual(
            Decimal(expected_price), invoices_models.Invoice.objects.first().price
        )

    def test_package_creation_does_not_increase_price_for_cheaper_1_day_long_old_package_in_the_end_of_the_month(
        self,
    ):
        old_component_price = 5
        new_component_price = old_component_price + 5
        start_date = timezone.datetime(2014, 2, 27, tzinfo=pytz.UTC)
        package_change_date = timezone.datetime(2014, 2, 28, tzinfo=pytz.UTC)
        end_of_the_month = core_utils.month_end(package_change_date)

        with freeze_time(start_date):
            old_package = invoices_fixtures.create_package(
                component_price=old_component_price
            )
        tenant = old_package.tenant

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = invoices_fixtures.create_package_template(
                component_price=new_component_price
            )
            new_package = packages_factories.OpenStackPackageFactory(
                template=new_template, tenant=tenant,
            )

        old_components_price = (
            old_package.template.price * (package_change_date - start_date).days
        )
        second_component_usage_days = invoices_utils.get_full_days(
            package_change_date, end_of_the_month
        )
        new_components_price = new_package.template.price * second_component_usage_days
        expected_price = old_components_price + new_components_price

        # assert
        self.assertEqual(invoices_models.Invoice.objects.count(), 1)
        self.assertEqual(
            Decimal(expected_price), invoices_models.Invoice.objects.first().price
        )

    def test_package_creation_does_not_increase_price_for_cheaper_1_day_long_old_package_in_the_same_day(
        self,
    ):
        old_component_price = 10
        new_component_price = old_component_price + 5
        start_date = timezone.datetime(2014, 2, 26, tzinfo=pytz.UTC)
        package_change_date = start_date
        end_of_the_month = core_utils.month_end(package_change_date)

        with freeze_time(start_date):
            old_package = invoices_fixtures.create_package(
                component_price=old_component_price
            )
        customer = old_package.tenant.service_project_link.project.customer

        with freeze_time(package_change_date):
            old_package.delete()
            new_template = invoices_fixtures.create_package_template(
                component_price=new_component_price
            )
            new_package = packages_factories.OpenStackPackageFactory(
                template=new_template,
                tenant__service_project_link__project__customer=customer,
            )

        old_components_price = (
            old_package.template.price * (package_change_date - start_date).days
        )
        second_component_usage_days = invoices_utils.get_full_days(
            package_change_date, end_of_the_month
        )
        new_components_price = new_package.template.price * second_component_usage_days
        expected_price = old_components_price + new_components_price

        # assert
        self.assertEqual(invoices_models.Invoice.objects.count(), 1)
        self.assertEqual(
            Decimal(expected_price), invoices_models.Invoice.objects.first().price
        )
