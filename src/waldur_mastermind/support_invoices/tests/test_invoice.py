from decimal import Decimal

import datetime
import six
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_core.core.utils import month_end
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support.tests.base import override_support_settings

from . import fixtures


@freeze_time('2018-01-01 00:00:00')
@override_support_settings(
    ENABLED=True,
    ACTIVE_BACKEND='waldur_mastermind.support.backend.basic:BasicBackend'
)
class InvoicesTest(test.APITransactionTestCase):
    def setUp(self):
        super(InvoicesTest, self).setUp()
        self.fixture = fixtures.SupportFixture()
        self.order_item = self.fixture.order_item
        marketplace_factories.ServiceProviderFactory(customer=self.order_item.offering.customer,
                                                     description='ServiceProvider\'s description')
        self.order_item_process(self.order_item)

    def test_create_invoice(self):
        invoice = self.get_invoice()
        self.assertEqual(invoice.total, self.fixture.plan.unit_price)

    def test_update_invoice_if_added_new_offering(self):
        self.order_item_process(self.fixture.new_order_item)

        invoice = self.get_invoice()
        self.assertEqual(invoice.total, self.fixture.plan.unit_price * 2)

    def test_terminate_offering(self):
        offering = self.order_item.resource.scope
        offering.terminate()

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
        resource = self.order_item.resource
        resource.plan = self.fixture.new_plan
        resource.save()

        new_start = datetime.datetime.now()
        end = month_end(new_start)

        old_item = invoices_models.GenericInvoiceItem.objects.get(
            project=resource.project,
            unit_price=Decimal(10),
            end=new_start,
        )
        self.assertTrue(self.fixture.plan.name in old_item.details['name'])

        new_item = invoices_models.GenericInvoiceItem.objects.get(
            project=resource.project,
            unit_price=Decimal(5),
            start=new_start,
            end=end,
        )
        self.assertTrue(self.fixture.new_plan.name in new_item.details['name'])

    def test_invoice_item_should_include_service_provider_info(self):
        invoice = self.get_invoice()
        details = invoice.items.first().details
        self.assertTrue('service_provider_name' in details.keys())
        self.assertEqual(details['service_provider_name'], self.order_item.offering.customer.name)
        self.assertTrue('service_provider_uuid' in details.keys())
        self.assertEqual(details['service_provider_uuid'],
                         six.text_type(self.order_item.offering.customer.serviceprovider.uuid))

    def order_item_process(self, order_item):
        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        order_item.refresh_from_db()
        order_item.order.approve()
        order_item.order.save()

        order_item.resource.scope.set_ok()

    def get_invoice(self):
        date = datetime.date.today()
        return invoices_models.Invoice.objects.get(
            customer=self.fixture.customer,
            month=date.month,
            year=date.year,
        )
