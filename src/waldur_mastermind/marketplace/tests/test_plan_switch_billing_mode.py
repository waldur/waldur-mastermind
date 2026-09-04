"""Switching a resource between a limit plan and a usage plan of one offering."""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.logging import models as logging_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures
from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import callbacks, models, utils
from waldur_mastermind.marketplace.billing_limit import LimitPeriodProcessor
from waldur_mastermind.marketplace.billing_usage import BillingUsageProcessor
from waldur_mastermind.marketplace.enums import (
    BillingModes,
    LimitPeriods,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_billing_mode import (
    make_openstack_offering,
)
from waldur_mastermind.marketplace.utils import import_current_usages
from waldur_mastermind.marketplace_openstack import processors as openstack_processors
from waldur_openstack import tasks as openstack_tasks
from waldur_openstack.tests import fixtures as openstack_fixtures

CREATED_AT = "2026-04-10 12:00:00"
SWITCHED_AT = "2026-04-20 12:00:00"
POLLED_AFTER_SWITCH = "2026-04-20 14:00:00"
LIMITS = {"cores": 4, "ram": 1024, "storage": 1024}
USAGES = {"cores": 4, "ram": 1024, "storage": 1024}


@freeze_time(CREATED_AT)
class PlanSwitchBillingModeBase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.offering = make_openstack_offering(customer=self.fixture.customer)
        self.limit_plan = self.make_plan(
            BillingModes.INHERIT, cores_price=Decimal("10")
        )
        self.usage_plan = self.make_plan(BillingModes.USAGE, cores_price=Decimal("0.5"))

    def make_plan(self, mode, cores_price):
        plan = factories.PlanFactory(
            offering=self.offering,
            unit=models.Plan.Units.PER_MONTH,
            unit_price=Decimal("0"),
            billing_mode=mode,
        )
        for component in self.offering.components.all():
            factories.PlanComponentFactory(
                plan=plan,
                component=component,
                price=cores_price if component.type == "cores" else Decimal("0"),
            )
        return plan

    def make_resource(self, plan, limits=LIMITS):
        resource = factories.ResourceFactory(
            offering=self.offering,
            plan=plan,
            project=self.project,
            limits=dict(limits),
            state=ResourceStates.CREATING,
        )
        callbacks.resource_creation_succeeded(resource)
        resource.refresh_from_db()
        return resource

    def switch(self, resource, plan):
        with freeze_time(SWITCHED_AT):
            resource.plan = plan
            resource.save()
        resource.refresh_from_db()
        return resource

    def cores_items(self, resource):
        return invoices_models.InvoiceItem.objects.filter(
            resource=resource,
            details__offering_component_type="cores",
            unit_price__gte=0,
        ).order_by("start")

    def cores_usages(self, resource):
        return models.ComponentUsage.objects.filter(
            resource=resource, component__type="cores"
        ).order_by("plan_period__start")

    def current_period(self, resource):
        return models.ResourcePlanPeriod.objects.get(resource=resource, end=None)

    def poll(self, resource, at, usages=USAGES):
        with freeze_time(at):
            import_current_usages(
                resource, dict(usages), hourly_accumulation=resource.is_usage_based
            )


class LimitToUsageSwitchTest(PlanSwitchBillingModeBase):
    def setUp(self):
        super().setUp()
        self.resource = self.make_resource(self.limit_plan)

    def test_limit_item_ends_at_the_switch_and_nothing_replaces_it(self):
        self.switch(self.resource, self.usage_plan)

        items = self.cores_items(self.resource)
        self.assertEqual(items.count(), 1)
        item = items.first()
        self.assertEqual(item.unit_price, Decimal("10"))
        self.assertEqual(item.end, parse_datetime(SWITCHED_AT))
        self.assertEqual(
            parse_datetime(item.details["resource_limit_periods"][-1]["end"]),
            parse_datetime(SWITCHED_AT),
        )

    def test_plan_periods_are_rotated(self):
        old_period = self.current_period(self.resource)
        self.switch(self.resource, self.usage_plan)

        old_period.refresh_from_db()
        self.assertEqual(old_period.end, parse_datetime(SWITCHED_AT))
        new_period = self.current_period(self.resource)
        self.assertEqual(new_period.plan, self.usage_plan)
        self.assertEqual(new_period.start, parse_datetime(SWITCHED_AT))

    def test_poll_clock_restarts_at_the_switch(self):
        component = self.offering.components.get(type="cores")
        models.ComponentUsagePollRecord.objects.create(
            resource=self.resource,
            component=component,
            last_poll_time=parse_datetime(CREATED_AT),
            raw_usage=Decimal("4"),
            elapsed_hours=Decimal("1"),
            increment=Decimal("4"),
            accumulated_total=Decimal("4"),
            billing_period=parse_datetime(CREATED_AT).date(),
        )
        self.switch(self.resource, self.usage_plan)
        record = models.ComponentUsagePollRecord.objects.get(
            resource=self.resource, component=component
        )
        self.assertEqual(record.last_poll_time, parse_datetime(SWITCHED_AT))

    def test_usage_accrues_in_a_fresh_row_and_is_priced_by_the_new_plan(self):
        # High-water mark under the limit plan before the switch.
        self.poll(self.resource, "2026-04-10 14:00:00", {"cores": 3})
        old_period = self.current_period(self.resource)

        self.switch(self.resource, self.usage_plan)
        self.assertTrue(self.resource.is_usage_based)
        self.poll(self.resource, POLLED_AFTER_SWITCH)

        rows = self.cores_usages(self.resource)
        self.assertEqual(rows.count(), 2)
        old_row, new_row = rows
        self.assertEqual(old_row.plan_period, old_period)
        self.assertEqual(old_row.usage, Decimal("3"))
        self.assertEqual(new_row.plan_period, self.current_period(self.resource))
        # No poll record existed before the switch: the first poll counts one
        # hour of the current value.
        self.assertEqual(new_row.usage, Decimal("4.00"))

        BillingUsageProcessor._run_billing(new_row, created=True)

        items = self.cores_items(self.resource)
        self.assertEqual(items.count(), 2)
        limit_item, usage_item = items
        self.assertEqual(limit_item.unit_price, Decimal("10"))
        self.assertEqual(usage_item.unit_price, Decimal("0.5"))
        # The usage line names the plan it is priced with.
        self.assertIn(self.usage_plan.name, usage_item.name)
        self.assertEqual(usage_item.details["plan_name"], self.usage_plan.name)
        self.assertEqual(usage_item.details["plan_uuid"], self.usage_plan.uuid.hex)
        # ...and the unit the usage was measured in under that plan.
        self.assertEqual(usage_item.measured_unit, "core-hours")
        self.assertEqual(limit_item.measured_unit, "cores")
        self.assertEqual(usage_item.quantity, Decimal("4.00"))
        self.assertEqual(
            usage_item.details["plan_period_uuid"],
            self.current_period(self.resource).uuid.hex,
        )

    def test_change_limits_no_longer_applies_under_the_usage_plan(self):
        self.switch(self.resource, self.usage_plan)
        self.assertFalse(self.resource.is_limit_based)
        self.assertEqual(self.offering.get_limit_components(self.usage_plan), {})


class SwitchBackWithinMonthTest(PlanSwitchBillingModeBase):
    """Reserved → pay as you go → reserved within one month."""

    def setUp(self):
        super().setUp()
        self.resource = self.make_resource(self.limit_plan)

    def test_limit_item_is_continued_not_duplicated(self):
        self.switch(self.resource, self.usage_plan)
        with freeze_time("2026-04-25 12:00:00"):
            self.resource.plan = self.limit_plan
            self.resource.save()
        items = self.cores_items(self.resource)
        # One monthly line for the reserved plan, with two limit periods.
        self.assertEqual(items.count(), 1)
        item = items.get()
        self.assertEqual(len(item.details["resource_limit_periods"]), 2)
        self.assertEqual(item.end.date(), parse_datetime("2026-04-30").date())
        self.assertEqual(item.start, parse_datetime(CREATED_AT))
        # A monthly plan charges the month's fee once, whatever the days.
        self.assertEqual(item.quantity, Decimal("4"))

    def test_closed_monthly_item_keeps_its_quantity(self):
        self.switch(self.resource, self.usage_plan)
        item = self.cores_items(self.resource).get()
        self.assertEqual(item.quantity, Decimal("4"))
        self.assertEqual(item.price, Decimal("40"))


class UsageToLimitSwitchTest(PlanSwitchBillingModeBase):
    def setUp(self):
        super().setUp()
        self.resource = self.make_resource(self.usage_plan)

    def test_usage_plan_creates_no_limit_items_on_creation(self):
        self.assertEqual(self.cores_items(self.resource).count(), 0)

    def test_usage_before_the_switch_keeps_the_old_price(self):
        self.poll(self.resource, "2026-04-10 14:00:00")
        old_row = self.cores_usages(self.resource).get()
        old_period = old_row.plan_period
        BillingUsageProcessor._run_billing(old_row, created=True)
        usage_item = self.cores_items(self.resource).get()
        self.assertEqual(usage_item.unit_price, Decimal("0.5"))
        self.assertEqual(usage_item.quantity, Decimal("4.00"))

        self.switch(self.resource, self.limit_plan)
        self.assertTrue(self.resource.is_limit_based)

        items = self.cores_items(self.resource)
        self.assertEqual(items.count(), 2)
        usage_item, limit_item = items
        self.assertEqual(usage_item.unit_price, Decimal("0.5"))
        self.assertEqual(limit_item.unit_price, Decimal("10"))
        self.assertEqual(limit_item.start, parse_datetime(SWITCHED_AT))

        # A poll under the limit plan tracks a high-water mark in its own row
        # and leaves the usage row of the previous period untouched.
        self.poll(self.resource, POLLED_AFTER_SWITCH, {"cores": 6})
        rows = self.cores_usages(self.resource)
        self.assertEqual(rows.count(), 2)
        old_row.refresh_from_db()
        self.assertEqual(old_row.plan_period, old_period)
        self.assertEqual(old_row.usage, Decimal("4.00"))
        new_row = rows.last()
        self.assertEqual(new_row.usage, Decimal("6"))

        BillingUsageProcessor._run_billing(new_row, created=True)
        usage_item.refresh_from_db()
        self.assertEqual(usage_item.quantity, Decimal("4.00"))
        self.assertEqual(self.cores_items(self.resource).count(), 2)


@freeze_time(CREATED_AT)
class PlanSwitchApiTest(PlanSwitchBillingModeBase):
    def setUp(self):
        super().setUp()
        for permission in (
            PermissionEnum.SWITCH_RESOURCE_PLAN,
            PermissionEnum.CREATE_ORDER,
            PermissionEnum.APPROVE_ORDER,
        ):
            CustomerRole.OWNER.add_permission(permission)

    def switch_plan(self, resource, plan):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_url(resource, "switch_plan")
        return self.client.post(
            url, {"plan": factories.PlanFactory.get_public_url(plan)}
        )

    def test_switch_to_limit_plan_is_refused_without_limits(self):
        resource = self.make_resource(self.usage_plan, limits={})
        response = self.switch_plan(resource, self.limit_plan)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("plan", response.data)

    def test_switch_to_limit_plan_with_limits_creates_an_order(self):
        resource = self.make_resource(self.usage_plan)
        response = self.switch_plan(resource, self.limit_plan)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = models.Order.objects.get(resource=resource, type=OrderTypes.UPDATE)
        self.assertEqual(order.old_plan, self.usage_plan)
        self.assertEqual(order.plan, self.limit_plan)

        details = self.client.get(factories.OrderFactory.get_url(order)).data
        self.assertEqual(details["old_plan_billing_mode"], "usage")
        self.assertEqual(details["new_plan_billing_mode"], "limit")
        self.assertEqual(details["order_subtype"], "plan_switch")

    def test_switch_to_usage_plan_needs_no_limits(self):
        resource = self.make_resource(self.limit_plan)
        response = self.switch_plan(resource, self.usage_plan)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


@freeze_time(CREATED_AT)
class PlanSwitchExposureTest(PlanSwitchBillingModeBase):
    def test_switch_logs_an_event_with_both_billing_modes(self):
        resource = self.make_resource(self.limit_plan)
        resource.set_state_updating()
        resource.save()
        factories.OrderFactory(
            project=self.project,
            offering=self.offering,
            resource=resource,
            plan=self.usage_plan,
            old_plan=self.limit_plan,
            type=OrderTypes.UPDATE,
            state=OrderStates.EXECUTING,
        )

        with mock.patch("waldur_mastermind.marketplace.callbacks.transaction"):
            callbacks.resource_update_succeeded(resource)

        resource.refresh_from_db()
        self.assertEqual(resource.plan, self.usage_plan)
        event = logging_models.Event.objects.get(
            event_type="marketplace_resource_plan_switched"
        )
        self.assertEqual(event.context["old_plan_billing"], "limit")
        self.assertEqual(event.context["new_plan_billing"], "usage")
        self.assertIn("usage billing", event.message)


@freeze_time(CREATED_AT)
class TenantCreationByPlanModeTest(PlanSwitchBillingModeBase):
    def setUp(self):
        super().setUp()
        self.offering.scope = openstack_fixtures.OpenStackFixture().settings
        self.offering.save()

    def make_order(self, plan, limits):
        return factories.OrderFactory(
            project=self.project,
            offering=self.offering,
            plan=plan,
            limits=limits,
            attributes={"name": "tenant"},
        )

    def test_usage_plan_order_without_limits_gets_default_quotas(self):
        order = self.make_order(self.usage_plan, limits={})
        payload = openstack_processors.TenantCreateProcessor(order).get_post_data()
        self.assertNotIn("quotas", payload)

    def test_limit_plan_order_without_limits_is_rejected(self):
        order = self.make_order(self.limit_plan, limits={})
        with self.assertRaises(Exception) as raised:
            openstack_processors.TenantCreateProcessor(order).get_post_data()
        self.assertIn("Quotas are required", str(raised.exception))

    def test_limit_plan_order_with_limits_pushes_quotas(self):
        order = self.make_order(self.limit_plan, limits=LIMITS)
        payload = openstack_processors.TenantCreateProcessor(order).get_post_data()
        self.assertEqual(payload["quotas"]["vcpu"], 4)


class UsagePollSelectionTest(PlanSwitchBillingModeBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.openstack = openstack_fixtures.OpenStackFixture()
        self.tenant = self.openstack.tenant
        self.tenant.state = CoreStates.OK
        self.tenant.save()
        self.offering.scope = self.openstack.settings
        self.offering.save()

    def make_tenant_resource(self, plan):
        return factories.ResourceFactory(
            offering=self.offering,
            plan=plan,
            project=self.project,
            scope=self.tenant,
            state=ResourceStates.OK,
        )

    @mock.patch("waldur_openstack.tasks.pull_tenant_usage_quotas.delay")
    def test_usage_plan_tenant_is_polled(self, pull):
        self.make_tenant_resource(self.usage_plan)
        openstack_tasks.tenant_usage_billing_poll()
        pull.assert_called_once_with(self.tenant.pk)

    @mock.patch("waldur_openstack.tasks.pull_tenant_usage_quotas.delay")
    def test_limit_plan_tenant_is_skipped(self, pull):
        self.make_tenant_resource(self.limit_plan)
        openstack_tasks.tenant_usage_billing_poll()
        pull.assert_not_called()


class ManualUsageReportAfterSwitchTest(PlanSwitchBillingModeBase):
    """A reported amount is the month's total, as the site agent sends it;
    the rows and invoice items still stay one per plan period."""

    def setUp(self):
        super().setUp()
        self.usage_plan_b = self.make_plan(BillingModes.USAGE, cores_price=Decimal("1"))
        self.resource = self.make_resource(self.usage_plan, limits={})

    def report(self, at, amount):
        self.client.force_authenticate(self.fixture.staff)
        with freeze_time(at):
            response = self.client.post(
                "/api/marketplace-component-usages/set_usage/",
                {
                    "resource": self.resource.uuid.hex,
                    "usages": [{"type": "cores", "amount": amount}],
                },
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_old_period_row_is_frozen_and_the_new_one_holds_the_rest(self):
        self.report("2026-04-15 12:00:00", 100)
        self.switch(self.resource, self.usage_plan_b)
        self.report("2026-04-25 12:00:00", 150)

        rows = self.cores_usages(self.resource)
        self.assertEqual(
            [(row.plan_period.plan_id, row.usage) for row in rows],
            [(self.usage_plan.id, Decimal(100)), (self.usage_plan_b.id, Decimal(50))],
        )

    def test_each_period_is_invoiced_at_its_own_price(self):
        self.report("2026-04-15 12:00:00", 100)
        self.switch(self.resource, self.usage_plan_b)
        self.report("2026-04-25 12:00:00", 150)
        for row in self.cores_usages(self.resource):
            BillingUsageProcessor._run_billing(row, created=True)

        items = self.cores_items(self.resource)
        self.assertEqual(
            [(item.quantity, item.unit_price) for item in items],
            [(Decimal(100), Decimal("0.5")), (Decimal(50), Decimal(1))],
        )

    def test_report_without_a_switch_keeps_one_row(self):
        self.report("2026-04-15 12:00:00", 100)
        self.report("2026-04-25 12:00:00", 150)
        row = self.cores_usages(self.resource).get()
        self.assertEqual(row.usage, Decimal(150))


class UsageLimitEnforcementAfterSwitchTest(PlanSwitchBillingModeBase):
    """Only rows billed like the current plan count towards its limits."""

    def test_core_hours_under_the_usage_plan_are_not_a_quota(self):
        resource = self.make_resource(self.limit_plan)
        self.poll(resource, CREATED_AT)
        self.switch(resource, self.usage_plan)
        self.poll(resource, POLLED_AFTER_SWITCH)
        self.poll(resource, "2026-04-20 16:00:00")
        resource.refresh_from_db()

        new_row = self.cores_usages(resource).last()
        self.assertGreaterEqual(new_row.usage, LIMITS["cores"])
        self.assertEqual(
            utils.get_current_period_usage(resource)["cores"], float(new_row.usage)
        )
        self.assertFalse(utils.is_usage_over_component_limit(resource))

    def test_core_hours_before_a_switch_to_the_limit_plan_are_ignored(self):
        resource = self.make_resource(self.usage_plan)
        self.poll(resource, CREATED_AT)
        self.poll(resource, "2026-04-10 14:00:00")
        self.switch(resource, self.limit_plan)
        self.poll(
            resource,
            POLLED_AFTER_SWITCH,
            usages={"cores": 2, "ram": 512, "storage": 512},
        )
        resource.refresh_from_db()

        self.assertEqual(utils.get_current_period_usage(resource)["cores"], 2.0)
        self.assertFalse(utils.is_usage_over_component_limit(resource))


class LimitChangeUnderLimitPlanTest(PlanSwitchBillingModeBase):
    """A limit change is billed with the plan's period, not the stored one."""

    def test_limit_change_continues_the_monthly_item(self):
        self.offering.components.filter(type="cores").update(
            limit_period=LimitPeriods.TOTAL
        )
        plan = self.make_plan(BillingModes.LIMIT, cores_price=Decimal("10"))
        resource = self.make_resource(plan)
        invoice = invoices_models.Invoice.objects.get(
            customer=self.fixture.customer, year=2026, month=4
        )

        with freeze_time(SWITCHED_AT):
            LimitPeriodProcessor.process_update(resource, invoice, "cores", 8)

        item = self.cores_items(resource).get()
        self.assertEqual(len(item.details["resource_limit_periods"]), 2)
        self.assertEqual(item.details["resource_limit_periods"][-1]["quantity"], 8)


class LegacyUsageItemAdoptionTest(PlanSwitchBillingModeBase):
    """Items created before per-period keys are adopted by their plan."""

    def test_item_without_period_key_is_adopted_after_a_switch(self):
        plan_b = self.make_plan(BillingModes.USAGE, cores_price=Decimal("1"))
        resource = self.make_resource(self.usage_plan, limits={})
        self.poll(resource, "2026-04-10 14:00:00")
        old_row = self.cores_usages(resource).get()
        BillingUsageProcessor._run_billing(old_row, created=True)
        item = self.cores_items(resource).get()
        del item.details["plan_period_uuid"]
        item.save(update_fields=["details"])

        self.switch(resource, plan_b)
        BillingUsageProcessor._run_billing(old_row, created=False)

        item = self.cores_items(resource).get()
        self.assertEqual(item.details["plan_period_uuid"], old_row.plan_period.uuid.hex)


class OrderListQueryCountTest(PlanSwitchBillingModeBase):
    def make_order(self):
        resource = self.make_resource(self.limit_plan)
        return factories.OrderFactory(
            offering=self.offering,
            project=self.project,
            resource=resource,
            plan=self.usage_plan,
            old_plan=self.limit_plan,
            type=OrderTypes.UPDATE,
        )

    def test_plan_billing_modes_do_not_add_queries_per_order(self):
        self.make_order()
        self.client.force_authenticate(self.fixture.staff)
        url = "/api/marketplace-orders/"
        with CaptureQueriesContext(connection) as one:
            self.client.get(url)
        self.make_order()
        self.make_order()
        with CaptureQueriesContext(connection) as three:
            response = self.client.get(url)
        self.assertEqual(len(response.data), 3)

        def component_queries(context):
            return [
                q["sql"]
                for q in context.captured_queries
                if q["sql"].startswith('SELECT "marketplace_offeringcomponent"')
            ]

        # The plans' components are prefetched once, not once per order.
        self.assertEqual(
            len(component_queries(one)),
            len(component_queries(three)),
            "\n".join(component_queries(three)),
        )
