from unittest import mock

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.utils import get_full_days
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.signals import resource_limit_update_succeeded
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_MODE_DYNAMIC,
    STORAGE_MODE_FIXED,
    STORAGE_TYPE,
)
from waldur_openstack.openstack_base.tests.fixtures import OpenStackFixture

from .. import TENANT_TYPE


@freeze_time('2019-09-10')
class BaseTenantInvoiceTest(test.APITransactionTestCase):
    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory(type=TENANT_TYPE)
        self.limits = {
            RAM_TYPE: 1 * 1024,
            CORES_TYPE: 2,
            STORAGE_TYPE: 3 * 1024,
        }
        self.prices = {
            RAM_TYPE: 10,
            CORES_TYPE: 100,
            STORAGE_TYPE: 1,
        }
        for ct in [RAM_TYPE, CORES_TYPE, STORAGE_TYPE]:
            marketplace_factories.OfferingComponentFactory(
                offering=self.offering, type=ct,
            )

    def get_unit_price(self, prices, limits):
        return (
            limits[RAM_TYPE] * prices[RAM_TYPE] / 1024
            + limits[CORES_TYPE] * prices[CORES_TYPE]
            + limits[STORAGE_TYPE] * prices[STORAGE_TYPE] / 1024
        )

    def create_plan(self, prices, unit=marketplace_models.Plan.Units.PER_DAY):
        plan = marketplace_factories.PlanFactory(offering=self.offering, unit=unit)
        for ct in prices.keys():
            marketplace_factories.PlanComponentFactory(
                plan=plan,
                component=self.offering.components.get(type=ct),
                price=prices[ct],
            )
        return plan

    def create_resource(
        self, prices, limits, unit=marketplace_models.Plan.Units.PER_DAY
    ) -> marketplace_models.Resource:
        plan = self.create_plan(prices, unit)
        resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            plan=plan,
            limits=limits,
            state=marketplace_models.Resource.States.CREATING,
        )
        callbacks.resource_creation_succeeded(resource)
        return resource

    def update_resource_limits(self, resource, new_limits):
        order = marketplace_factories.OrderFactory(
            project=resource.project, state=marketplace_models.Order.States.EXECUTING,
        )
        order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=self.offering,
            resource=resource,
            type=marketplace_models.OrderItem.Types.UPDATE,
            state=marketplace_models.OrderItem.States.EXECUTING,
            limits=new_limits,
        )
        resource_limit_update_succeeded.send(
            sender=resource.__class__, order_item=order_item
        )

    def switch_plan(self, resource, prices, unit=marketplace_models.Plan.Units.PER_DAY):
        order = marketplace_factories.OrderFactory(
            project=resource.project, state=marketplace_models.Order.States.EXECUTING,
        )
        new_plan = self.create_plan(prices, unit)
        marketplace_factories.OrderItemFactory(
            order=order,
            offering=self.offering,
            resource=resource,
            type=marketplace_models.OrderItem.Types.UPDATE,
            state=marketplace_models.OrderItem.States.EXECUTING,
            plan=new_plan,
        )
        callbacks.resource_update_succeeded(resource)

    def delete_resource(self, resource):
        callbacks.resource_deletion_succeeded(resource)


class TenantInvoiceTest(BaseTenantInvoiceTest):
    def test_when_resource_is_created_invoice_is_updated(self):
        resource = self.create_resource(self.prices, self.limits)
        invoice_item = invoices_models.InvoiceItem.objects.get(resource=resource)
        expected_price = self.get_unit_price(self.prices, self.limits)
        self.assertEqual(invoice_item.unit_price, expected_price)

    def test_when_resource_limits_are_updated_invoice_item_is_updated(self):
        new_limits = {
            RAM_TYPE: 10 * 1024,
            CORES_TYPE: 20,
            STORAGE_TYPE: 30 * 1024,
        }
        with freeze_time('2017-01-01'):
            resource = self.create_resource(self.prices, self.limits)

        with freeze_time('2017-01-10'):
            self.update_resource_limits(resource, new_limits)

        invoice_items = invoices_models.InvoiceItem.objects.filter(resource=resource)

        self.assertEqual(invoice_items.count(), 2)
        self.assertNotEqual(
            invoice_items.last().unit_price, invoice_items.first().unit_price
        )
        expected_price = self.get_unit_price(self.prices, new_limits)
        self.assertEqual(
            invoice_items.last().unit_price, expected_price,
        )

    @freeze_time('2017-01-01')
    def test_plan_with_monthly_pricing(self):
        resource = self.create_resource(
            self.prices, self.limits, unit=UnitPriceMixin.Units.PER_MONTH
        )

        invoice = invoices_models.Invoice.objects.get(
            customer=resource.project.customer
        )
        self.assertEqual(invoice.price, self.get_unit_price(self.prices, self.limits))

    def test_change_from_daily_to_monthly_plan(self):
        """
        Monthly plan is ignored in invoicing because it is cheaper than daily plan.
        """
        with freeze_time('2017-01-01'):
            resource = self.create_resource(
                self.prices, self.limits, unit=UnitPriceMixin.Units.PER_DAY
            )

        with freeze_time('2017-01-10'):
            self.switch_plan(resource, self.prices, UnitPriceMixin.Units.PER_MONTH)

        invoice = invoices_models.Invoice.objects.get(
            customer=resource.project.customer
        )
        self.assertEqual(
            invoice.price, self.get_unit_price(self.prices, self.limits) * 31
        )

    def test_switch_to_more_expensive_plan(self):
        new_prices = {
            RAM_TYPE: 50,
            CORES_TYPE: 500,
            STORAGE_TYPE: 5,
        }
        with freeze_time('2017-01-01'):
            resource = self.create_resource(self.prices, self.limits)

        with freeze_time('2017-01-10'):
            self.switch_plan(resource, new_prices)

        old_plan_total = self.get_unit_price(self.prices, self.limits) * 9
        new_plan_total = self.get_unit_price(new_prices, self.limits) * 22
        invoice = invoices_models.Invoice.objects.get(
            customer=resource.project.customer
        )
        self.assertEqual(invoice.price, old_plan_total + new_plan_total)

    def test_switch_to_more_cheap_plan(self):
        new_prices = {
            RAM_TYPE: 5,
            CORES_TYPE: 10,
            STORAGE_TYPE: 1,
        }

        with freeze_time('2017-01-01'):
            resource = self.create_resource(self.prices, self.limits)

        with freeze_time('2017-01-10'):
            self.switch_plan(resource, new_prices)

        old_plan_total = self.get_unit_price(self.prices, self.limits) * 10
        new_plan_total = self.get_unit_price(new_prices, self.limits) * 21
        invoice = invoices_models.Invoice.objects.get(
            customer=resource.project.customer
        )
        self.assertEqual(invoice.price, old_plan_total + new_plan_total)

    def test_price_is_calculated_properly_if_it_was_used_only_for_one_day(self):
        date = timezone.datetime(2017, 1, 26)
        month_end = timezone.datetime(2017, 1, 31, 23, 59, 59)
        full_days = get_full_days(date, month_end)

        low_prices = {
            RAM_TYPE: 5,
            CORES_TYPE: 10,
            STORAGE_TYPE: 1,
        }
        high_prices = {
            RAM_TYPE: 50,
            CORES_TYPE: 500,
            STORAGE_TYPE: 5,
        }

        # at first user has bought cheap package
        with freeze_time(date):
            resource = self.create_resource(low_prices, self.limits)

        invoice = invoices_models.Invoice.objects.get(
            customer=resource.project.customer
        )
        cheap_item = invoice.items.get(resource=resource)
        self.assertEqual(
            cheap_item.unit_price, self.get_unit_price(low_prices, self.limits)
        )
        self.assertEqual(cheap_item.usage_days, full_days)

        # later at the same day he switched to the expensive one
        with freeze_time(date + timezone.timedelta(hours=2)):
            self.switch_plan(resource, high_prices)

        expensive_item = invoice.items.get(resource=resource)
        self.assertEqual(
            expensive_item.unit_price, self.get_unit_price(high_prices, self.limits)
        )
        self.assertEqual(expensive_item.usage_days, full_days)

        # at last he switched to the medium one
        with freeze_time(date + timezone.timedelta(hours=4)):
            self.switch_plan(resource, self.prices)

        medium_item = invoice.items.filter(resource=resource).last()
        # medium item usage days should start from tomorrow,
        # because expensive item should be calculated for current day
        self.assertEqual(medium_item.usage_days, full_days - 1)
        # expensive item should be calculated for one day
        expensive_item.refresh_from_db()

        # cheap item price should not exits
        self.assertRaises(ObjectDoesNotExist, cheap_item.refresh_from_db)

    def test_when_resource_is_deleted_invoice_is_updated(self):
        resource = self.create_resource(self.prices, self.limits)
        with freeze_time('2019-09-18'):
            self.delete_resource(resource)
        invoice_item = invoices_models.InvoiceItem.objects.get(resource=resource)
        self.assertEqual(invoice_item.end.day, 18)


class StorageModeInvoiceTest(BaseTenantInvoiceTest):
    def setUp(self):
        # Arrange
        super(StorageModeInvoiceTest, self).setUp()
        fixture = OpenStackFixture()
        tenant = fixture.openstack_tenant
        offering_component = marketplace_models.OfferingComponent.objects.create(
            offering=self.offering, type='gigabytes_gpfs'
        )

        plan = self.create_plan(self.prices)
        marketplace_models.PlanComponent.objects.create(
            component=offering_component, plan=plan, price=10,
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            plan=plan,
            limits=self.limits,
            state=marketplace_models.Resource.States.CREATING,
        )

        callbacks.resource_creation_succeeded(self.resource)
        self.resource.scope = tenant
        self.resource.save()
        tenant.set_quota_limit('vcpu', 6)
        tenant.set_quota_limit('ram', 10 * 1024)
        tenant.set_quota_usage('storage', 30 * 1024)
        tenant.set_quota_usage('gigabytes_gpfs', 100 * 1024)

    def test_when_storage_mode_is_switched_to_dynamic_limits_are_updated(self):
        # Act
        with freeze_time('2019-09-20'):
            self.offering.plugin_options['storage_mode'] = STORAGE_MODE_DYNAMIC
            self.offering.save()

        # Assert
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits.get('cores'), 6)
        self.assertEqual(self.resource.limits.get('ram'), 10 * 1024)
        self.assertEqual(self.resource.limits.get('storage'), None)
        self.assertEqual(self.resource.limits.get('gigabytes_gpfs'), 100 * 1024)

        invoice_item = invoices_models.InvoiceItem.objects.filter(
            resource=self.resource
        ).last()
        self.assertTrue('102400 GB gpfs storage' in invoice_item.name)

    def test_when_storage_mode_is_switched_to_fixed_limits_are_updated(self):
        # Act
        with freeze_time('2019-09-20'):
            self.offering.plugin_options['storage_mode'] = STORAGE_MODE_FIXED
            self.offering.save()

        # Assert
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits.get('cores'), 6)
        self.assertEqual(self.resource.limits.get('ram'), 10 * 1024)
        self.assertEqual(self.resource.limits.get('storage'), 30 * 1024)
        self.assertEqual(self.resource.limits.get('gigabytes_gpfs'), None)

        invoice_item = invoices_models.InvoiceItem.objects.filter(
            resource=self.resource
        ).last()
        self.assertTrue('30 GB storage' in invoice_item.name)

    @mock.patch(
        'waldur_mastermind.marketplace_openstack.utils.import_limits_when_storage_mode_is_switched'
    )
    def test_when_storage_mode_is_not_switched_limits_are_not_updated(
        self, mocked_utils
    ):
        # Act
        with freeze_time('2019-09-20'):
            self.offering.plugin_options['FOO'] = 'BAR'
            self.offering.save()

        # Assert
        self.assertEqual(mocked_utils.call_count, 0)
