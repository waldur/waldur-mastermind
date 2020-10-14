import math
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import utils as invoices_utils
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm import models as slurm_models
from waldur_slurm.tests import factories as slurm_factories
from waldur_slurm.tests import fixtures as slurm_fixtures

from .. import models


class InvoicesTest(TestCase):
    def setUp(self):
        self.fixture = slurm_fixtures.SlurmFixture()

    def get_expected_quantity(self, usage, component_type):
        usage = getattr(usage, component_type + '_usage')
        if component_type == 'ram':
            expected_quantity = int(math.ceil(1.0 * usage / 1024 / 60))
        else:
            expected_quantity = int(math.ceil(1.0 * usage / 60))
        return expected_quantity

    def test_invoice_item_is_not_created_if_package_does_not_exist(self):
        with self.assertRaises(ObjectDoesNotExist):
            invoice_models.InvoiceItem.objects.get(scope=self.fixture.allocation)

    def test_invoice_item_is_not_created_if_usage_does_not_exist(self):
        models.SlurmPackage.objects.create(
            service_settings=self.fixture.service.settings
        )
        invoice_items = self.get_invoice_items()
        self.assertEqual(len(invoice_items), 0)

    def test_invoice_item_price_is_updated_when_allocation_usage_is_changed(self):
        package = self.create_package()
        allocation_usage = self.update_usage()
        invoice_items = self.get_invoice_items()
        self.assertEqual(len(invoice_items), self.get_component_number())

        for invoice_item in invoice_items:
            item_type = invoice_item.details['type']
            if item_type == 'ram':
                expected_price = (
                    int(math.ceil(1.0 * allocation_usage.ram_usage / 1024 / 60))
                    * package.ram_price
                )
            else:
                usage = item_type + '_usage'
                price = item_type + '_price'
                expected_price = int(
                    math.ceil(1.0 * getattr(allocation_usage, usage) / 60)
                ) * getattr(package, price)
            self.assertEqual(invoice_item.price, expected_price)

    def test_when_allocation_is_cancelled_invoice_item_is_terminated(self):
        self.create_package()
        allocation_usage = self.update_usage()
        allocation = allocation_usage.allocation

        allocation.is_active = False
        allocation.save()

        invoice_items = self.get_invoice_items()
        component_details = self.get_component_details()

        self.assertEqual(len(invoice_items), self.get_component_number())

        for invoice_item in invoice_items:
            item_type = invoice_item.details['type']
            expected_name = f'{allocation.name} ({component_details[item_type]["name"]} {component_details[item_type]["unit"]})'

            self.assertEqual(invoice_item.name, expected_name)
            self.assertEqual(
                invoice_item.quantity,
                self.get_expected_quantity(allocation_usage, item_type),
            )

    def test_invoice_item_name_format(self):
        self.create_package()
        allocation = self.fixture.allocation
        invoice_items = self.get_invoice_items()
        self.update_usage()
        component_details = self.get_component_details()

        self.assertEqual(len(invoice_items), self.get_component_number())

        for invoice_item in invoice_items:
            item_type = invoice_item.details['type']

            expected_name = f'{allocation.name} ({component_details[item_type]["name"]} {component_details[item_type]["unit"]})'

            self.assertEqual(invoice_item.name, expected_name)

    def test_invoice_item_quantity(self):
        self.create_package()
        invoice_items = self.get_invoice_items()

        self.assertEqual(len(invoice_items), 0)

        allocation_usage_after_update = self.update_usage()
        invoice_items_after_update = self.get_invoice_items()

        self.assertEqual(len(invoice_items_after_update), self.get_component_number())

        for invoice_item in invoice_items_after_update:
            item_type = invoice_item.details['type']

            self.assertEqual(
                invoice_item.quantity,
                self.get_expected_quantity(allocation_usage_after_update, item_type),
            )

    def test_invoice_items_partial_creation(self):
        self.create_package()
        allocation_usage = self.update_usage(gpu_usage=0)
        used_components = self.get_component_details()
        used_components.pop('gpu')
        invoice_items = self.get_invoice_items()

        self.assertEqual(len(invoice_items), len(used_components))

        for invoice_item in invoice_items:
            item_type = invoice_item.details['type']
            self.assertTrue(item_type in used_components)
            self.assertEqual(
                invoice_item.quantity,
                self.get_expected_quantity(allocation_usage, item_type),
            )

        allocation_usage_after_update = self.update_usage(
            cpu_usage=allocation_usage.cpu_usage + 100,
            ram_usage=allocation_usage.ram_usage + 1024,
        )
        invoice_items_after_update = self.get_invoice_items()
        self.assertEqual(len(invoice_items_after_update), self.get_component_number())

        for invoice_item in invoice_items_after_update:
            item_type = invoice_item.details['type']
            self.assertEqual(
                invoice_item.quantity,
                self.get_expected_quantity(allocation_usage_after_update, item_type),
            )

    def test_invoice_item_usages_reset_for_new_month(self):
        self.create_package()
        with freeze_time('2020-05-31'):
            self.update_usage()
            invoice_items = self.get_invoice_items()
            self.assertEqual(len(invoice_items), self.get_component_number())

        with freeze_time('2020-06-01'):
            invoice_items = self.get_invoice_items().filter(
                start=invoices_utils.get_current_month_start(),
                end=invoices_utils.get_current_month_end(),
            )
            self.assertEqual(len(invoice_items), 0)  # Because new month starts

    def create_package(self):
        package = models.SlurmPackage.objects.create(
            service_settings=self.fixture.service.settings
        )
        package.cpu_price = Decimal(0.400)
        package.gpu_price = Decimal(0.200)
        package.ram_price = Decimal(0.100)
        package.save()
        package.refresh_from_db()
        return package

    def update_usage(self, cpu_usage=6001, gpu_usage=12001, ram_usage=2048):
        allocation = self.fixture.allocation
        allocation.cpu_usage = cpu_usage
        allocation.gpu_usage = gpu_usage
        allocation.ram_usage = ram_usage  # in MB
        allocation.save()
        now = timezone.now()
        existing_usage = slurm_models.AllocationUsage.objects.filter(
            allocation=allocation, month=now.month, year=now.year,
        ).first()

        if existing_usage:
            existing_usage.cpu_usage = allocation.cpu_usage
            existing_usage.gpu_usage = allocation.gpu_usage
            existing_usage.ram_usage = allocation.ram_usage
            existing_usage.save()
            return existing_usage

        allocation_usage = slurm_factories.AllocationUsageFactory(
            allocation=allocation,
            cpu_usage=allocation.cpu_usage,
            gpu_usage=allocation.gpu_usage,
            ram_usage=allocation.ram_usage,
            month=now.month,
            year=now.year,
        )
        return allocation_usage

    def get_invoice_items(self):
        return invoice_models.InvoiceItem.objects.filter(scope=self.fixture.allocation)

    def get_component_details(self):
        return {
            component.type: {'name': component.name, 'unit': component.measured_unit}
            for component in manager.get_components(PLUGIN_NAME)
        }

    def get_component_number(self):
        return len(manager.get_components(PLUGIN_NAME))
