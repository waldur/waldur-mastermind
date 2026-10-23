import datetime
from decimal import Decimal
from unittest import mock

from ddt import data, ddt, unpack
from freezegun import freeze_time
from rest_framework import serializers as rf_serializers
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import (
    callbacks,
    models,
    serializers,
    spending_limits,
    tasks,
    utils,
)
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.utils import evaluate_usage_limit_restriction

SET_USAGE_URL = "/api/marketplace-component-usages/set_usage/"
ALL_PERIODS = {
    "day": {"default": 20, "maximum": 100},
    "week": {"default": 100},
    "month": {"default": 300, "maximum": 1000},
    "quarter": {},
    "year": {},
}


def utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.UTC)


class PeriodBoundsTest(test.APITestCase):
    def test_week_starts_on_monday_and_crosses_months(self):
        start, end = spending_limits.period_bounds("week", datetime.date(2026, 10, 1))
        self.assertEqual(start, datetime.date(2026, 9, 28))
        self.assertEqual(end, datetime.date(2026, 10, 5))

    def test_quarter_and_year(self):
        day = datetime.date(2026, 11, 15)
        self.assertEqual(
            spending_limits.period_bounds("quarter", day),
            (datetime.date(2026, 10, 1), datetime.date(2027, 1, 1)),
        )
        self.assertEqual(
            spending_limits.period_bounds("year", day),
            (datetime.date(2026, 1, 1), datetime.date(2027, 1, 1)),
        )

    def test_day_resets_at_utc_midnight(self):
        self.assertEqual(
            spending_limits.resets_at("day", datetime.date(2026, 10, 21)),
            utc(2026, 10, 22),
        )


@ddt
class ComponentPeriodsValidationTest(test.APITestCase):
    def test_cleans_amounts(self):
        cleaned = spending_limits.validate_component_periods(
            {"day": {"default": "20", "maximum": 100.5}, "month": None},
            BillingTypes.USAGE,
        )
        self.assertEqual(
            cleaned, {"day": {"default": 20, "maximum": 100.5}, "month": {}}
        )

    @data(BillingTypes.LIMIT, BillingTypes.FIXED, BillingTypes.ONE_TIME)
    def test_rejected_on_other_billing_types(self, billing_type):
        with self.assertRaises(rf_serializers.ValidationError):
            spending_limits.validate_component_periods({"day": {}}, billing_type)

    @data(
        {"hour": {}},
        {"day": {"default": 200, "maximum": 100}},
        {"day": {"default": -1}},
        {"day": {"default": "1.234"}},
        {"day": {"minimum": 1}},
        {"day": {"default": 50}, "week": {"default": 40}},
        {"day": "20"},
    )
    def test_rejects_invalid_periods(self, value):
        with self.assertRaises(rf_serializers.ValidationError):
            spending_limits.validate_component_periods(value, BillingTypes.USAGE)


class SpendingLimitsSetupMixin:
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
            plugin_options={"action_on_usage_limit": "pause"},
        )
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="token_cost",
            spending_limit_periods=ALL_PERIODS,
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(
            plan=self.plan, component=self.component, price=1
        )
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=self.plan,
        )
        callbacks.resource_creation_succeeded(self.resource)
        self.resource.refresh_from_db()

    def set_limits(self, **periods):
        self.resource.spending_limits = {"token_cost": periods}
        self.resource.save(update_fields=["spending_limits"])

    def add_day(self, day, usage):
        return models.ComponentDailyUsage.objects.create(
            resource=self.resource, component=self.component, date=day, usage=usage
        )

    def add_month(self, billing_period, usage):
        return factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=usage,
            date=utc(billing_period.year, billing_period.month, 1),
            billing_period=billing_period,
        )


@ddt
@freeze_time("2026-10-21 12:00")
class ResourceLimitsValidationTest(SpendingLimitsSetupMixin, test.APITestCase):
    def validate(self, value):
        return spending_limits.validate_resource_limits(value, self.offering, self.plan)

    def test_null_clears_a_period_and_empty_components_are_dropped(self):
        self.assertEqual(
            self.validate({"token_cost": {"day": 10, "month": None}}),
            {"token_cost": {"day": 10}},
        )
        self.assertEqual(self.validate({"token_cost": {"day": None}}), {})

    def test_null_clears_a_component_that_no_longer_takes_limits(self):
        self.assertEqual(self.validate({"ram": {"day": None}}), {})

    def test_bare_null_clears_a_component_that_no_longer_takes_limits(self):
        self.assertEqual(self.validate({"ram": None}), {})

    @data(
        {"token_cost": {"day": 101}},
        {"token_cost": {"day": 50, "week": 40}},
        {"token_cost": {"hour": 1}},
        {"token_cost": {"day": -1}},
        {"other": {"day": 1}},
    )
    def test_rejects(self, value):
        with self.assertRaises(rf_serializers.ValidationError):
            self.validate(value)

    def test_null_clears_a_period_no_longer_offered(self):
        self.assertEqual(
            self.validate({"token_cost": {"hour": None, "day": 5}}),
            {"token_cost": {"day": 5}},
        )

    def test_rejects_component_without_offered_periods(self):
        factories.OfferingComponentFactory(
            offering=self.offering, billing_type=BillingTypes.USAGE, type="ram"
        )
        with self.assertRaises(rf_serializers.ValidationError):
            self.validate({"ram": {"month": 1}})


class ComponentDialogTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.component = factories.OfferingComponentFactory(
            offering=self.offering, billing_type=BillingTypes.USAGE, type="token_cost"
        )
        self.client.force_authenticate(self.fixture.staff)

    def update(self, **fields):
        return self.client.post(
            factories.OfferingFactory.get_url(
                self.offering, "update_offering_component"
            ),
            {"uuid": self.component.uuid.hex, **fields},
            format="json",
        )

    def test_provider_enables_spending_limits(self):
        response = self.update(
            spending_limit_periods={"day": {"maximum": 50}, "month": {}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.component.refresh_from_db()
        self.assertEqual(
            self.component.spending_limit_periods,
            {"day": {"maximum": 50}, "month": {}},
        )

    def test_misspelt_bound_is_rejected(self):
        response = self.update(spending_limit_periods={"day": {"maximun": 50}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_renaming_the_component_moves_resource_limits(self):
        self.component.spending_limit_periods = {"day": {}}
        self.component.save()
        resource = factories.ResourceFactory(
            offering=self.offering, spending_limits={"token_cost": {"day": 20}}
        )
        response = self.update(type="spend")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        resource.refresh_from_db()
        self.assertEqual(resource.spending_limits, {"spend": {"day": 20}})

    def test_rejected_on_limit_based_component(self):
        response = self.update(
            billing_type=BillingTypes.LIMIT, spending_limit_periods={"month": {}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_switching_billing_type_requires_clearing_them(self):
        self.component.spending_limit_periods = {"month": {}}
        self.component.save()
        response = self.update(billing_type=BillingTypes.LIMIT)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.update(
            billing_type=BillingTypes.LIMIT, spending_limit_periods={}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_rejected_when_a_resource_option_shows_its_limit(self):
        self.offering.resource_options = {
            "options": {
                "quota": {
                    "type": "integer",
                    "label": "Quota",
                    "display_limit_config": {"component_type": "token_cost"},
                }
            },
            "order": ["quota"],
        }
        self.offering.save()
        response = self.update(spending_limit_periods={"month": {}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resource_option_cannot_show_limit_of_capped_component(self):
        self.component.spending_limit_periods = {"month": {}}
        self.component.save()
        response = self.client.post(
            factories.OfferingFactory.get_url(self.offering, "update_resource_options"),
            {
                "resource_options": {
                    "options": {
                        "quota": {
                            "type": "integer",
                            "label": "Quota",
                            "display_limit_config": {"component_type": "token_cost"},
                        }
                    },
                    "order": ["quota"],
                }
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@freeze_time("2026-10-21 12:00")
class OrderSpendingLimitsTest(SpendingLimitsSetupMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)

    def create_order(self, spending_limits_value):
        self.client.force_authenticate(self.fixture.owner)
        return self.client.post(
            factories.OrderFactory.get_list_url(),
            {
                "project": structure_factories.ProjectFactory.get_url(
                    self.fixture.project
                ),
                "offering": factories.OfferingFactory.get_public_url(self.offering),
                "plan": factories.PlanFactory.get_public_url(self.plan),
                "attributes": {"name": "inference"},
                "spending_limits": spending_limits_value,
            },
            format="json",
        )

    def test_limits_are_stored_on_the_resource_not_in_limits(self):
        response = self.create_order({"token_cost": {"day": 20, "month": 300}})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(
            order.resource.spending_limits, {"token_cost": {"day": 20, "month": 300}}
        )
        self.assertEqual(order.resource.limits, {})
        self.assertEqual(order.limits, {})

    def test_rejects_value_above_maximum(self):
        response = self.create_order({"token_cost": {"day": 500}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("spending_limits", response.data)

    def test_rejects_shorter_period_above_longer(self):
        response = self.create_order({"token_cost": {"day": 90, "week": 50}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
@freeze_time("2026-10-21 12:00")
class SetSpendingLimitsActionTest(SpendingLimitsSetupMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        self.url = factories.ResourceFactory.get_url(
            self.resource, "set_spending_limits"
        )

    def post(self, user, value):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.post(self.url, {"spending_limits": value}, format="json")

    @data("staff", "owner", "admin", "manager")
    def test_authorized_user_sets_limits_without_an_order(self, user):
        response = self.post(user, {"token_cost": {"day": 30}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.spending_limits, {"token_cost": {"day": 30}})
        self.assertEqual(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.UPDATE
            ).count(),
            0,
        )

    @unpack
    @data(("member", status.HTTP_403_FORBIDDEN), ("user", status.HTTP_404_NOT_FOUND))
    def test_other_users_cannot(self, user, expected_status):
        # A user outside the project cannot see the resource at all.
        response = self.post(user, {"token_cost": {"day": 30}})
        self.assertEqual(response.status_code, expected_status)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.spending_limits, {})

    def test_null_component_clears_its_limits(self):
        self.set_limits(day=10)
        response = self.post("owner", {"token_cost": None})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.spending_limits, {})

    def test_value_above_maximum_is_rejected(self):
        response = self.post("owner", {"token_cost": {"day": 101}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_is_logged(self):
        self.post("owner", {"token_cost": {"day": 30}})
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_SPENDING_LIMITS_UPDATED
            ).exists()
        )

    def test_clearing_limits_lifts_the_restriction_at_once(self):
        self.set_limits(day=10)
        self.add_day(datetime.date(2026, 10, 21), 15)
        evaluate_usage_limit_restriction(self.resource)
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

        response = self.post("owner", {})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)
        self.assertEqual(self.resource.usage_limit_restriction, "")

    def test_lowering_a_limit_restricts_at_once(self):
        self.add_day(datetime.date(2026, 10, 21), 15)
        self.post("owner", {"token_cost": {"day": 10}})
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        self.assertEqual(self.resource.usage_limit_resumes_at, utc(2026, 10, 22))


@freeze_time("2026-10-21 12:00")
class DailyUsageReportTest(SpendingLimitsSetupMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.fixture.staff)

    def report_days(self, days, usages=None):
        return self.client.post(
            SET_USAGE_URL,
            {
                "resource": self.resource.uuid.hex,
                "usages": usages or [],
                "daily_usages": [
                    {"type": "token_cost", "date": day, "amount": amount}
                    for day, amount in days
                ],
            },
            format="json",
        )

    def month_rows(self):
        return models.ComponentUsage.objects.filter(
            resource=self.resource,
            component=self.component,
            billing_period=datetime.date(2026, 10, 1),
        )

    def test_month_row_is_the_sum_of_days(self):
        response = self.report_days([("2026-10-19", 18), ("2026-10-20", 20)])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            models.ComponentDailyUsage.objects.filter(resource=self.resource).count(),
            2,
        )
        row = self.month_rows().get()
        self.assertEqual(row.usage, Decimal(38))
        self.assertEqual(row.plan_period.plan, self.plan)

    def test_days_are_split_per_plan_after_a_switch(self):
        new_plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(plan=new_plan, component=self.component, price=2)
        old_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource,
            plan=self.plan,
            start=utc(2026, 10, 1),
            end=utc(2026, 10, 20),
        )
        new_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=new_plan, start=utc(2026, 10, 20)
        )
        self.resource.plan = new_plan
        self.resource.save()

        response = self.report_days(
            [("2026-10-18", 4), ("2026-10-19", 6), ("2026-10-20", 5)]
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        rows = {row.plan_period_id: row.usage for row in self.month_rows()}
        self.assertEqual(rows, {old_period.id: Decimal(10), new_period.id: Decimal(5)})
        items = invoice_models.InvoiceItem.objects.filter(
            resource=self.resource, details__offering_component_type="token_cost"
        )
        self.assertEqual(
            sorted((item.quantity, item.unit_price) for item in items),
            [(Decimal(5), Decimal(2)), (Decimal(10), Decimal(1))],
        )

    def test_month_total_of_a_plan_without_days_is_zeroed(self):
        old_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan, start=utc(2026, 9, 1)
        )
        self.client.post(
            SET_USAGE_URL,
            {
                "resource": self.resource.uuid.hex,
                "usages": [{"type": "token_cost", "amount": 100}],
            },
            format="json",
        )
        new_plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(plan=new_plan, component=self.component)
        old_period.end = utc(2026, 10, 1)
        old_period.save()
        new_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=new_plan, start=utc(2026, 10, 1)
        )

        self.report_days([("2026-10-21", 7)])

        rows = {row.plan_period_id: row.usage for row in self.month_rows()}
        self.assertEqual(rows, {old_period.id: Decimal(0), new_period.id: Decimal(7)})

    def test_resubmitting_a_day_replaces_it(self):
        self.report_days([("2026-10-19", 18), ("2026-10-20", 20)])
        self.report_days([("2026-10-20", 5)])
        self.assertEqual(
            models.ComponentDailyUsage.objects.get(
                resource=self.resource, date=datetime.date(2026, 10, 20)
            ).usage,
            Decimal(5),
        )
        self.assertEqual(self.month_rows().get().usage, Decimal(23))

    def test_invoice_item_matches_the_days(self):
        self.report_days([("2026-10-19", 18), ("2026-10-20", 20)])
        item = invoice_models.InvoiceItem.objects.get(
            resource=self.resource, details__offering_component_type="token_cost"
        )
        self.assertEqual(item.quantity, Decimal(38))

    def test_month_total_replaced_by_first_daily_report(self):
        response = self.client.post(
            SET_USAGE_URL,
            {
                "resource": self.resource.uuid.hex,
                "usages": [{"type": "token_cost", "amount": 100}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.report_days([("2026-10-21", 7)])
        self.assertEqual(self.month_rows().get().usage, Decimal(7))

    def test_monthly_report_rejected_for_month_with_days(self):
        self.report_days([("2026-10-19", 18)])
        response = self.client.post(
            SET_USAGE_URL,
            {
                "resource": self.resource.uuid.hex,
                "usages": [{"type": "token_cost", "amount": 100}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.month_rows().get().usage, Decimal(18))

    def test_monthly_and_daily_of_one_component_in_one_report_rejected(self):
        response = self.report_days(
            [("2026-10-19", 18)], usages=[{"type": "token_cost", "amount": 100}]
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_future_and_duplicate_dates(self):
        self.assertEqual(
            self.report_days([("2026-10-22", 1)]).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.report_days([("2026-10-20", 1), ("2026-10-20", 2)]).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_rejects_more_items_than_one_report_may_hold(self):
        start = datetime.date(2023, 1, 1)
        days = [
            ((start + datetime.timedelta(days=i)).isoformat(), 1)
            for i in range(serializers.MAX_DAILY_USAGE_ITEMS + 1)
        ]
        response = self.report_days(days)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("daily_usages", response.data)
        self.assertFalse(
            models.ComponentDailyUsage.objects.filter(resource=self.resource).exists()
        )

    def test_rejects_daily_usage_of_limit_based_component(self):
        factories.OfferingComponentFactory(
            offering=self.offering, billing_type=BillingTypes.LIMIT, type="cores"
        )
        response = self.client.post(
            SET_USAGE_URL,
            {
                "resource": self.resource.uuid.hex,
                "usages": [],
                "daily_usages": [{"type": "cores", "date": "2026-10-20", "amount": 1}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty_report_is_still_accepted(self):
        response = self.report_days([])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_staff_may_report_a_day_of_a_past_month(self):
        response = self.report_days([("2026-09-30", 1)])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_past_month_needs_a_mutable_invoice_for_a_provider(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        self.client.force_authenticate(self.fixture.owner)
        response = self.report_days([("2026-09-30", 1)])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_report_of_a_capped_component_schedules_evaluation(self):
        self.set_limits(day=20)
        with (
            mock.patch(
                "waldur_mastermind.marketplace.tasks."
                "evaluate_usage_limit_restriction_task.delay"
            ) as mock_delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.report_days([("2026-10-21", 20)])
        mock_delay.assert_called_with(self.resource.pk)

    def test_monthly_report_of_a_capped_component_schedules_evaluation(self):
        self.set_limits(month=300)
        with (
            mock.patch(
                "waldur_mastermind.marketplace.tasks."
                "evaluate_usage_limit_restriction_task.delay"
            ) as mock_delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(
                SET_USAGE_URL,
                {
                    "resource": self.resource.uuid.hex,
                    "usages": [{"type": "token_cost", "amount": 300}],
                },
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_delay.assert_called_once_with(self.resource.pk)

    def test_report_without_spending_limits_schedules_nothing(self):
        with (
            mock.patch(
                "waldur_mastermind.marketplace.tasks."
                "evaluate_usage_limit_restriction_task.delay"
            ) as mock_delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.report_days([("2026-10-21", 20)])
        mock_delay.assert_not_called()


@freeze_time("2026-10-21 12:00")
class EnforcementTest(SpendingLimitsSetupMixin, test.APITestCase):
    def evaluate(self):
        evaluate_usage_limit_restriction(self.resource)
        self.resource.refresh_from_db()

    def test_daily_limit_pauses_until_midnight(self):
        self.set_limits(day=20, month=300)
        self.add_day(datetime.date(2026, 10, 21), 20)
        self.evaluate()
        self.assertTrue(self.resource.paused)
        self.assertEqual(self.resource.usage_limit_resumes_at, utc(2026, 10, 22))

        with freeze_time("2026-10-22 00:03"):
            tasks.lift_reset_spending_limit_restrictions()
        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)
        self.assertEqual(self.resource.usage_limit_restriction, "")
        self.assertIsNone(self.resource.usage_limit_resumes_at)

    def test_reset_task_skips_restrictions_not_yet_due(self):
        self.set_limits(day=20)
        self.add_day(datetime.date(2026, 10, 21), 20)
        self.evaluate()
        with freeze_time("2026-10-21 23:59"):
            tasks.lift_reset_spending_limit_restrictions()
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

    def test_below_every_limit_is_not_restricted(self):
        self.set_limits(day=20, week=100, month=300)
        self.add_day(datetime.date(2026, 10, 21), 19)
        self.evaluate()
        self.assertFalse(self.resource.paused)

    @freeze_time("2026-10-02 12:00")
    def test_week_counts_days_of_the_previous_month(self):
        self.set_limits(week=50)
        self.add_day(datetime.date(2026, 9, 28), 30)
        self.add_day(datetime.date(2026, 10, 1), 20)
        self.evaluate()
        self.assertTrue(self.resource.paused)
        self.assertEqual(self.resource.usage_limit_resumes_at, utc(2026, 10, 5))

    def test_month_limit_reads_the_monthly_rows(self):
        self.set_limits(month=300)
        self.add_month(datetime.date(2026, 10, 1), 300)
        self.evaluate()
        self.assertTrue(self.resource.paused)
        self.assertEqual(self.resource.usage_limit_resumes_at, utc(2026, 11, 1))

    def test_quarter_and_year_sum_their_months(self):
        self.set_limits(quarter=500, year=900)
        self.add_month(datetime.date(2026, 2, 1), 450)
        self.add_month(datetime.date(2026, 10, 1), 450)
        self.evaluate()
        self.assertTrue(self.resource.paused)
        reached = {s["period"] for s in spending_limits.reached_limits(self.resource)}
        self.assertEqual(reached, {"year"})
        self.assertEqual(self.resource.usage_limit_resumes_at, utc(2027, 1, 1))

    def test_resumes_when_every_reached_limit_has_reset(self):
        self.set_limits(day=20, month=40)
        self.add_day(datetime.date(2026, 10, 21), 20)
        self.add_month(datetime.date(2026, 10, 1), 40)
        self.evaluate()
        self.assertEqual(self.resource.usage_limit_resumes_at, utc(2026, 11, 1))
        with freeze_time("2026-10-22 00:03"):
            tasks.lift_reset_spending_limit_restrictions()
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

    def test_resume_time_moves_when_a_longer_limit_is_reached_later(self):
        self.set_limits(day=20, month=40)
        self.add_day(datetime.date(2026, 10, 21), 20)
        self.evaluate()
        self.assertEqual(self.resource.usage_limit_resumes_at, utc(2026, 10, 22))
        self.add_month(datetime.date(2026, 10, 1), 40)
        self.evaluate()
        self.assertTrue(self.resource.paused)
        self.assertEqual(self.resource.usage_limit_resumes_at, utc(2026, 11, 1))

    def test_downscale(self):
        self.offering.plugin_options = {"action_on_usage_limit": "downscale"}
        self.offering.save()
        self.set_limits(day=20)
        self.add_day(datetime.date(2026, 10, 21), 25)
        self.evaluate()
        self.assertTrue(self.resource.downscaled)
        self.assertFalse(self.resource.paused)

    def test_manual_pause_is_not_taken_over_or_lifted(self):
        self.set_limits(day=20)
        self.resource.paused = True
        self.resource.save(update_fields=["paused"])
        self.add_day(datetime.date(2026, 10, 21), 25)
        self.evaluate()
        self.assertTrue(self.resource.paused)
        self.assertEqual(self.resource.usage_limit_restriction, "")

        with freeze_time("2026-10-22 00:03"):
            tasks.lift_reset_spending_limit_restrictions()
            self.evaluate()
        self.assertTrue(self.resource.paused)

    def test_decides_from_the_committed_limits_not_the_callers_copy(self):
        # A task loaded the resource before the customer raised the limit.
        self.set_limits(day=10)
        self.add_day(datetime.date(2026, 10, 21), 15)
        stale = models.Resource.objects.get(pk=self.resource.pk)
        models.Resource.objects.filter(pk=self.resource.pk).update(
            spending_limits={"token_cost": {"day": 50}}
        )
        evaluate_usage_limit_restriction(stale)
        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)

    def test_firing_cost_policy_keeps_the_pause_after_the_reset(self):
        self.set_limits(day=20)
        self.add_day(datetime.date(2026, 10, 21), 20)
        self.evaluate()
        self.assertTrue(self.resource.paused)

        with (
            mock.patch.object(utils, "PAUSE_KEEPERS", [lambda resource: True]),
            freeze_time("2026-10-22 00:03"),
        ):
            tasks.lift_reset_spending_limit_restrictions()
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        self.assertEqual(self.resource.usage_limit_restriction, "paused")
        self.assertIsNone(self.resource.usage_limit_resumes_at)

    def test_reset_task_skips_terminating_resources(self):
        self.set_limits(day=20)
        self.add_day(datetime.date(2026, 10, 21), 20)
        self.evaluate()
        self.resource.state = models.Resource.States.TERMINATING
        self.resource.save(update_fields=["state"])
        with (
            mock.patch(
                "waldur_mastermind.marketplace.tasks.evaluate_usage_limit_restriction"
            ) as mock_evaluate,
            freeze_time("2026-10-22 00:03"),
        ):
            tasks.lift_reset_spending_limit_restrictions()
        mock_evaluate.assert_not_called()

    def test_limit_component_keeps_resume_time_unknown(self):
        cores = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.LIMIT,
            type="cores",
            limit_amount=10,
        )
        factories.ComponentUsageFactory(
            resource=self.resource,
            component=cores,
            usage=10,
            date=utc(2026, 10, 21),
            billing_period=datetime.date(2026, 10, 1),
        )
        self.set_limits(day=20)
        self.add_day(datetime.date(2026, 10, 21), 20)
        self.evaluate()
        self.assertTrue(self.resource.paused)
        self.assertIsNone(self.resource.usage_limit_resumes_at)

    def test_not_enforced_while_plan_bills_component_by_limit(self):
        self.component.billed_per_plan = True
        self.component.save()
        self.plan.billing_mode = "limit"
        self.plan.save()
        self.set_limits(day=20)
        self.add_day(datetime.date(2026, 10, 21), 50)
        self.evaluate()
        self.assertFalse(self.resource.paused)

    def test_spending_limits_never_create_invoice_items(self):
        self.set_limits(day=20, month=300)
        self.evaluate()
        self.assertFalse(
            invoice_models.InvoiceItem.objects.filter(resource=self.resource).exists()
        )


@freeze_time("2026-10-21 12:00")
class StatusApiTest(SpendingLimitsSetupMixin, test.APITestCase):
    def test_status_per_component_and_period(self):
        self.set_limits(day=20, month=300)
        self.add_day(datetime.date(2026, 10, 21), 20)
        self.add_month(datetime.date(2026, 10, 1), 142)
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(factories.ResourceFactory.get_url(self.resource))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["spending_limits"], {"token_cost": {"day": 20, "month": 300}}
        )
        rows = {row["period"]: row for row in response.data["spending_limit_status"]}
        self.assertEqual(set(rows), {"day", "month"})
        self.assertEqual(rows["day"]["used"], 20.0)
        self.assertTrue(rows["day"]["reached"])
        self.assertEqual(rows["day"]["resets_at"], utc(2026, 10, 22))
        self.assertEqual(rows["month"]["used"], 142.0)
        self.assertFalse(rows["month"]["reached"])
        self.assertIsNotNone(rows["month"]["last_reported_at"])

    def test_resource_without_limits_has_no_status(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.ResourceFactory.get_url(self.resource))
        self.assertEqual(response.data["spending_limit_status"], [])
        self.assertIsNone(response.data["usage_limit_resumes_at"])


@freeze_time("2026-10-21 12:00")
class TotalLimitTest(SpendingLimitsSetupMixin, test.APITestCase):
    """A total limit counts every month since the resource started and never resets."""

    def setUp(self):
        super().setUp()
        self.component.spending_limit_periods = {
            **ALL_PERIODS,
            "total": {"maximum": 5000},
        }
        self.component.save(update_fields=["spending_limit_periods"])
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)

    def evaluate(self):
        evaluate_usage_limit_restriction(self.resource)
        self.resource.refresh_from_db()

    def set_spending_limits(self, value):
        self.client.force_authenticate(self.fixture.owner)
        return self.client.post(
            factories.ResourceFactory.get_url(self.resource, "set_spending_limits"),
            {"spending_limits": value},
            format="json",
        )

    def test_has_no_reset(self):
        self.assertIsNone(
            spending_limits.resets_at("total", datetime.date(2026, 10, 21))
        )

    def test_is_the_longest_period(self):
        with self.assertRaises(rf_serializers.ValidationError):
            spending_limits.validate_order(
                {"year": Decimal(600), "total": Decimal(500)}, "Limits"
            )

    def test_provider_offers_it_with_a_maximum(self):
        self.assertEqual(
            spending_limits.validate_component_periods(
                {"total": {"default": 500, "maximum": 5000}}, BillingTypes.USAGE
            ),
            {"total": {"default": 500, "maximum": 5000}},
        )

    def test_resource_takes_it_only_where_offered(self):
        self.assertEqual(
            spending_limits.validate_resource_limits(
                {"token_cost": {"year": 400, "total": 500}}, self.offering, self.plan
            ),
            {"token_cost": {"year": 400, "total": 500}},
        )
        with self.assertRaises(rf_serializers.ValidationError):
            spending_limits.validate_resource_limits(
                {"token_cost": {"total": 5001}}, self.offering, self.plan
            )
        self.component.spending_limit_periods = ALL_PERIODS
        self.component.save(update_fields=["spending_limit_periods"])
        with self.assertRaises(rf_serializers.ValidationError):
            spending_limits.validate_resource_limits(
                {"token_cost": {"total": 500}}, self.offering, self.plan
            )

    def test_counts_every_month_since_the_resource_started(self):
        self.add_month(datetime.date(2024, 5, 1), 100)
        self.add_month(datetime.date(2025, 12, 1), 150)
        self.add_month(datetime.date(2026, 10, 1), 50)
        self.assertEqual(
            spending_limits.period_usage(
                self.resource, self.component, "total", datetime.date(2026, 10, 21)
            ),
            Decimal(300),
        )

    def test_reached_total_pauses_and_never_lifts_on_its_own(self):
        self.set_limits(total=300)
        self.add_month(datetime.date(2025, 12, 1), 250)
        self.add_month(datetime.date(2026, 10, 1), 50)
        self.evaluate()
        self.assertTrue(self.resource.paused)
        self.assertIsNone(self.resource.usage_limit_resumes_at)

        with freeze_time("2027-02-01 00:03"):
            tasks.lift_reset_spending_limit_restrictions()
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

    def test_a_reached_total_holds_past_a_shorter_reset(self):
        self.set_limits(day=20, total=300)
        self.add_day(datetime.date(2026, 10, 21), 20)
        self.add_month(datetime.date(2026, 10, 1), 300)
        self.evaluate()
        self.assertTrue(self.resource.paused)
        self.assertIsNone(self.resource.usage_limit_resumes_at)

    def test_raising_the_total_lifts_at_once(self):
        self.set_limits(total=300)
        self.add_month(datetime.date(2026, 10, 1), 300)
        self.evaluate()
        self.assertTrue(self.resource.paused)

        response = self.set_spending_limits({"token_cost": {"total": 400}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)
        self.assertEqual(self.resource.usage_limit_restriction, "")

    def test_status_has_no_reset_time(self):
        self.set_limits(month=200, total=500)
        self.add_month(datetime.date(2026, 9, 1), 300)
        self.add_month(datetime.date(2026, 10, 1), 142)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.ResourceFactory.get_url(self.resource))
        rows = {row["period"]: row for row in response.data["spending_limit_status"]}
        self.assertEqual(rows["total"]["used"], 442.0)
        self.assertFalse(rows["total"]["reached"])
        self.assertIsNone(rows["total"]["resets_at"])
        self.assertEqual(rows["month"]["resets_at"], utc(2026, 11, 1))
