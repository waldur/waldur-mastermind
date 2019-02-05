from decimal import Decimal

import datetime
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.core import utils as core_utils
from waldur_core.core.utils import month_end
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests.base import BaseTest


@freeze_time('2018-01-01 00:00:00')
class InvoicesTest(BaseTest):
    def setUp(self):
        super(InvoicesTest, self).setUp()
        self.offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME, options={'order': []})
        self.order_item = marketplace_factories.OrderItemFactory(
            offering=self.offering,
            attributes={'name': 'item_name', 'description': 'Description'}
        )
        self.offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=marketplace_models.OfferingComponent.BillingTypes.FIXED)

        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.order_item.plan,
            component=self.offering_component
        )

        self.order_item_process(self.order_item)

    def test_create_invoice(self):
        invoice = self.get_invoice()
        self.assertTrue(invoice.total)
        self.assertEqual(invoice.total, self.plan_component.price * self.plan_component.amount)

    def test_update_invoice_if_added_new_offering(self):
        self.order_2 = marketplace_factories.OrderFactory(project=self.order_item.order.project)
        self.order_item_2 = marketplace_factories.OrderItemFactory(
            offering=self.offering,
            attributes={'name': 'item_name_2', 'description': 'Description_2'},
            plan=self.order_item.plan,
            order=self.order_2
        )
        self.order_item_process(self.order_item_2)
        invoice = self.get_invoice()
        self.assertTrue(invoice.total)
        self.assertEqual(invoice.total, (self.plan_component.price * self.plan_component.amount) * 2)

    def test_terminate_offering(self):
        offering = self.order_item.resource.scope
        offering.state = support_models.Offering.States.TERMINATED
        offering.save()
        invoice_item = invoices_models.GenericInvoiceItem.objects.get(scope=offering)
        self.assertEqual(invoice_item.end, timezone.now())

    def test_delete_offering(self):
        offering = self.order_item.resource.scope
        invoice_item = invoices_models.GenericInvoiceItem.objects.get(scope=offering)
        offering.delete()
        invoice_item.refresh_from_db()
        self.assertEqual(invoice_item.end, timezone.now())

    @freeze_time('2018-01-15 00:00:00')
    def test_switch_plan_resource(self):
        new_plan = marketplace_factories.PlanFactory(offering=self.offering)
        marketplace_factories.PlanComponentFactory(
            plan=new_plan,
            component=self.offering_component,
            price=Decimal(5)
        )
        resource = self.order_item.resource
        resource.plan = new_plan
        resource.save()
        new_start = datetime.datetime.now()
        end = month_end(new_start)
        self.assertTrue(invoices_models.GenericInvoiceItem.objects.filter(
            project=resource.project,
            unit_price=Decimal(10),
            end=new_start,
        ).exists())
        self.assertTrue(invoices_models.GenericInvoiceItem.objects.filter(
            project=resource.project,
            unit_price=Decimal(5),
            start=new_start,
            end=end,
        ).exists())

    def order_item_process(self, order_item):
        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        order_item.refresh_from_db()
        order_item.order.approve()
        order_item.order.save()

        order_item.resource.scope.state = support_models.Offering.States.OK
        order_item.resource.scope.save()

    def get_invoice(self):
        date = datetime.date.today()
        return invoices_models.Invoice.objects.get(
            customer=self.order_item.order.project.customer,
            month=date.month,
            year=date.year,
        )
