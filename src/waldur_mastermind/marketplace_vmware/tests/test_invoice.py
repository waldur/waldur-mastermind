from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_vmware import VIRTUAL_MACHINE_TYPE
from waldur_vmware import signals
from waldur_vmware.tests.fixtures import VMwareFixture


@freeze_time('2019-07-01')
class InvoiceTest(test.APITransactionTestCase):

    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory(type=VIRTUAL_MACHINE_TYPE)
        self.plan = marketplace_factories.PlanFactory(
            offering=self.offering, unit=UnitPriceMixin.Units.PER_DAY,
        )

        for component_type in ('cpu', 'ram', 'disk'):
            marketplace_factories.PlanComponentFactory(
                plan=self.plan,
                component=marketplace_factories.OfferingComponentFactory(
                    offering=self.offering,
                    type=component_type
                )
            )

        self.fixture = VMwareFixture()
        self.vm = self.fixture.virtual_machine
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            scope=self.vm,
            project=self.fixture.project,
        )

    def test_when_vm_is_created_invoice_item_is_registered(self):
        # Act
        signals.vm_created.send(self.__class__, vm=self.vm)

        # Assert
        invoice = invoices_models.Invoice.objects.get(customer=self.fixture.customer)
        self.assertEqual(1, invoice.items.count())

        item = invoice.items.get()
        self.assertEqual(item.scope, self.vm)

    def test_when_disk_is_created_invoice_total_is_increased(self):
        # Arrange
        signals.vm_created.send(self.__class__, vm=self.vm)
        invoice = invoices_models.Invoice.objects.get(customer=self.fixture.customer)
        old_total = invoice.total
        self.assertGreater(old_total, 0)

        # Act
        with freeze_time('2019-07-10'):
            self.fixture.disk
            signals.vm_updated.send(self.__class__, vm=self.vm)

            # Assert
            self.assertEqual(2, invoice.items.count())
            new_total = invoice.total
            self.assertGreater(new_total, old_total)

    def test_when_vm_is_upgraded_invoice_item_is_registered(self):
        # Arrange
        signals.vm_created.send(self.__class__, vm=self.vm)
        invoice = invoices_models.Invoice.objects.get(customer=self.fixture.customer)
        old_total = invoice.total
        self.assertGreater(old_total, 0)

        # Act
        with freeze_time('2019-07-10'):
            self.vm.cores += 1
            self.vm.save()
            signals.vm_updated.send(self.__class__, vm=self.vm)

            # Assert
            self.assertEqual(2, invoice.items.count())
            new_total = invoice.total
            self.assertGreater(new_total, old_total)

            self.assertEqual(invoice.items.first().end.day, 9)
            self.assertEqual(invoice.items.last().start.day, 10)

    def test_when_vm_is_downgraded_invoice_item_is_adjusted(self):
        # Arrange
        signals.vm_created.send(self.__class__, vm=self.vm)
        invoice = invoices_models.Invoice.objects.get(customer=self.fixture.customer)

        # Act
        with freeze_time('2019-07-10'):
            self.vm.cores -= 1
            self.vm.save()
            signals.vm_updated.send(self.__class__, vm=self.vm)

            # Assert
            self.assertEqual(invoice.items.first().end.day, 10)
            self.assertEqual(invoice.items.last().start.day, 11)

    def test_when_vm_is_deleted_all_invoice_items_are_terminated(self):
        # Arrange
        signals.vm_created.send(self.__class__, vm=self.vm)
        invoice = invoices_models.Invoice.objects.get(customer=self.fixture.customer)

        # Act
        with freeze_time('2019-07-10'):
            self.vm.cores -= 1
            self.vm.save()
            signals.vm_updated.send(self.__class__, vm=self.vm)

        with freeze_time('2019-07-12'):
            self.vm.delete()

        # Assert
        self.assertEqual(invoice.items.first().end.day, 10)
        self.assertEqual(invoice.items.last().end.day, 12)
