import math
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.test import TestCase

from waldur_mastermind.invoices import models as invoice_models
from waldur_slurm.tests import fixtures as slurm_fixtures

from .. import models


class InvoicesTest(TestCase):
    def setUp(self):
        self.fixture = slurm_fixtures.SlurmFixture()

    def test_invoice_item_is_not_created_if_package_does_not_exist(self):
        with self.assertRaises(ObjectDoesNotExist):
            invoice_models.InvoiceItem.objects.get(scope=self.fixture.allocation)

    def test_invoice_item_is_created_if_package_exists(self):
        models.SlurmPackage.objects.create(
            service_settings=self.fixture.service.settings
        )
        invoice_item = self.get_invoice_item()
        self.assertEqual(invoice_item.unit_price, 0)

    def test_invoice_item_price_is_updated_when_allocation_usage_is_changed(self):
        package = self.create_package()
        allocation = self.update_usage()
        invoice_item = self.get_invoice_item()

        expected_price = (
            int(math.ceil(1.0 * allocation.cpu_usage / 60)) * package.cpu_price
            + int(math.ceil(1.0 * allocation.gpu_usage / 60)) * package.gpu_price
            + int(math.ceil(1.0 * allocation.ram_usage / 1024)) * package.ram_price
        )
        self.assertEqual(invoice_item.unit_price, expected_price)

    def test_when_allocation_is_cancelled_invoice_item_is_terminated(self):
        self.create_package()
        allocation = self.update_usage()

        allocation.is_active = False
        allocation.save()

        invoice_item = self.get_invoice_item()
        expected_name = (
            f'{allocation.name} (CPU: 6001 hours, GPU: 12001 hours, RAM: 2048 GB)'
        )

        self.assertEqual(invoice_item.name, expected_name)
        self.assertEqual(
            invoice_item.details,
            {
                'cpu_usage': allocation.cpu_usage,
                'gpu_usage': allocation.gpu_usage,
                'ram_usage': allocation.ram_usage,
                'scope_uuid': allocation.uuid.hex,
                'deposit_usage': '0',
            },
        )

    def test_invoice_item_name_format(self):
        self.create_package()
        allocation = self.fixture.allocation
        allocation.save()

        invoice_item = self.get_invoice_item()

        expected_name = allocation.name
        self.assertEqual(invoice_item.name, expected_name)

        allocation_after_update = self.update_usage()
        invoice_item_after_update = self.get_invoice_item()

        expected_name_after_update = (
            f'{allocation.name} (CPU: 6001 hours, GPU: 12001 hours, RAM: 2048 GB)'
        )
        self.assertEqual(invoice_item_after_update.name, expected_name_after_update)

        allocation_after_update.cpu_usage = 0
        allocation_after_update.save()
        invoice_item_after_update = self.get_invoice_item()
        expected_name_after_update = (
            f'{allocation.name} (GPU: 12001 hours, RAM: 2048 GB)'
        )
        self.assertEqual(invoice_item_after_update.name, expected_name_after_update)

        allocation_after_update.gpu_usage = 0
        allocation_after_update.save()
        invoice_item_after_update = self.get_invoice_item()
        expected_name_after_update = f'{allocation.name} (RAM: 2048 GB)'
        self.assertEqual(invoice_item_after_update.name, expected_name_after_update)

        allocation_after_update.ram_usage = 0
        allocation_after_update.gpu_usage = 10
        allocation_after_update.cpu_usage = 10
        allocation_after_update.save()
        invoice_item_after_update = self.get_invoice_item()
        expected_name_after_update = f'{allocation.name} (CPU: 10 hours, GPU: 10 hours)'
        self.assertEqual(invoice_item_after_update.name, expected_name_after_update)

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

    def update_usage(self):
        allocation = self.fixture.allocation
        allocation.cpu_usage = 6001
        allocation.gpu_usage = 12001
        allocation.ram_usage = 2048 * 2 ** 30
        allocation.save()
        return allocation

    def get_invoice_item(self):
        return invoice_models.InvoiceItem.objects.get(scope=self.fixture.allocation)
