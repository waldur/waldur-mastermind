import datetime
from decimal import Decimal

from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tasks import create_monthly_invoices
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    LimitPeriods,
    OfferingStates,
    OrderStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import CORES_TYPE
from waldur_mastermind.marketplace_openstack.tests.test_invoice import (
    BaseTenantInvoiceTest,
)


def set_limits(resource, limits):
    """Change limits the way set_limits and backend sync do: without an order."""
    resource.refresh_from_db()
    resource.limits = limits
    resource.save()


class LimitChangeWithoutOrderTest(BaseTenantInvoiceTest):
    def cores_items(self, resource):
        return invoices_models.InvoiceItem.objects.filter(
            resource=resource, details__offering_component_type=CORES_TYPE
        )

    def create_resource_with_order(self, limits, unit=None):
        kwargs = {"unit": unit} if unit else {}
        resource = self.create_resource(self.prices, limits, **kwargs)
        marketplace_factories.OrderFactory(
            project=resource.project,
            offering=self.offering,
            resource=resource,
            type=OrderTypes.CREATE,
            state=OrderStates.DONE,
            limits=limits,
        )
        return resource

    def test_raise_from_zero_after_the_create_order_had_a_non_zero_limit(self):
        with freeze_time("2019-08-01"):
            resource = self.create_resource_with_order(self.limits)
        with freeze_time("2019-08-10"):
            set_limits(resource, {**self.limits, CORES_TYPE: 0})
        with freeze_time("2019-09-01"):
            create_monthly_invoices()
        self.assertFalse(
            self.cores_items(resource).filter(invoice__month=9).exists(),
            "A zero limit must not be invoiced in the new month",
        )

        with freeze_time("2019-09-15"):
            set_limits(resource, {**self.limits, CORES_TYPE: 4})

        item = self.cores_items(resource).get(invoice__month=9)
        self.assertEqual(item.start.day, 15)
        # 4 cores for the 16 days from the 15th to the 30th
        self.assertEqual(item.quantity, 64)

    def test_raise_from_unlimited_is_invoiced_from_the_day_of_the_change(self):
        with freeze_time("2019-09-01"):
            resource = self.create_resource(
                self.prices, {**self.limits, CORES_TYPE: -1}
            )
        self.assertFalse(self.cores_items(resource).exists())

        with freeze_time("2019-09-15"):
            set_limits(resource, {**self.limits, CORES_TYPE: 4})

        item = self.cores_items(resource).get()
        self.assertEqual(item.start.day, 15)
        self.assertEqual(item.quantity, 64)

    def test_raise_from_empty_limits_is_invoiced(self):
        with freeze_time("2019-09-01"):
            resource = self.create_resource(self.prices, {})

        with freeze_time("2019-09-15"):
            set_limits(resource, {CORES_TYPE: 4})

        item = self.cores_items(resource).get()
        self.assertEqual(item.start.day, 15)
        self.assertEqual(item.quantity, 64)

    def test_monthly_plan_raise_from_zero_keeps_the_billing_period(self):
        with freeze_time("2019-09-01"):
            resource = self.create_resource(
                self.prices,
                {**self.limits, CORES_TYPE: 0},
                unit=marketplace_models.Plan.Units.PER_MONTH,
            )

        with freeze_time("2019-09-15"):
            set_limits(resource, {**self.limits, CORES_TYPE: 4})
        with freeze_time("2019-09-20"):
            set_limits(resource, {**self.limits, CORES_TYPE: 8})

        item = self.cores_items(resource).get()
        self.assertEqual(item.start.day, 1)
        # A later change is weighted over the whole month, not over the days
        # since the limit left zero: 4 cores for 19 days, 8 cores for 11 days.
        self.assertAlmostEqual(float(item.quantity), (4 * 19 + 8 * 11) / 30, places=2)


@freeze_time("2024-01-15")
class PrepaidLimitChangeWithoutOrderTest(test.APITestCase):
    def create_resource(self, storage):
        offering = marketplace_factories.OfferingFactory(
            type=BASIC_OFFERING, state=OfferingStates.ACTIVE
        )
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
            type="storage",
        )
        plan = marketplace_factories.PlanFactory(
            offering=offering,
            unit=marketplace_models.Plan.Units.PER_MONTH,
            unit_price=Decimal("0"),
        )
        marketplace_factories.PlanComponentFactory(
            plan=plan, component=component, price=Decimal("4.5")
        )
        resource = marketplace_factories.ResourceFactory(
            offering=offering,
            plan=plan,
            project=offering.customer.projects.create(name="test-project"),
            limits={"storage": storage},
            end_date=datetime.date(2024, 4, 15),
        )
        resource.set_state_ok()
        resource.save()
        marketplace_factories.OrderFactory(
            project=resource.project,
            offering=offering,
            resource=resource,
            plan=plan,
            type=OrderTypes.CREATE,
            state=OrderStates.DONE,
            limits={"storage": storage},
        )
        return resource

    def supplementary_quantities(self, resource):
        return sorted(
            item.quantity
            for item in invoices_models.InvoiceItem.objects.filter(resource=resource)
            if item.details.get("is_supplementary")
        )

    def test_second_raise_after_raise_from_zero_bills_only_the_difference(self):
        resource = self.create_resource(0)

        set_limits(resource, {"storage": 10})
        set_limits(resource, {"storage": 12})

        # 10 and then 2 more for the 3 months left of the term
        self.assertEqual(self.supplementary_quantities(resource), [6, 30])

    def test_second_raise_from_a_non_zero_limit_bills_only_the_difference(self):
        resource = self.create_resource(50)

        set_limits(resource, {"storage": 60})
        set_limits(resource, {"storage": 70})

        # 10 and then 10 more for the 3 months left of the term
        self.assertEqual(self.supplementary_quantities(resource), [30, 30])
