"""What resource API keys spent over time, with charts: the insights action."""

import datetime
import json
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings
from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.core import utils as core_utils
from waldur_core.core.chart_spec import MAX_SERIES, ChartSpecSerializer
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ServiceProviderRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import api_key_cost, models
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture

States = models.ResourceApiKey.States
URL = reverse("marketplace-resource-api-key-insights")


@freeze_time("2026-11-20 12:00:00")
class ApiKeyInsightsTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.offering = self.resource.offering
        self.offering.plugin_options = {"enable_api_key_provisioning": True}
        self.offering.save()
        self.cost_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="token_cost",
            name="Inference cost",
            measured_unit="USD",
            billing_type=BillingTypes.USAGE,
        )
        self.tokens_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="input_tokens",
            name="Input tokens",
            measured_unit="tokens",
            billing_type=BillingTypes.USAGE,
        )
        self.cost_plan_component = factories.PlanComponentFactory(
            plan=self.resource.plan, component=self.cost_component, price=Decimal("1")
        )
        factories.PlanComponentFactory(
            plan=self.resource.plan,
            component=self.tokens_component,
            price=Decimal("0"),
        )
        self.today = core_utils.utc_today()
        self.thesis = self.make_key("cid-1", "thesis-runs")
        self.batch = self.make_key("cid-2", "batch", state=States.PAUSED)

    def make_key(self, client_id, description, state=States.OK, **kwargs):
        resource = kwargs.pop("resource", self.resource)
        return models.ResourceApiKey.objects.create(
            resource=resource,
            client_id=client_id,
            description=description,
            state=state,
            **kwargs,
        )

    def set_month_limit(self, amount, component_type="token_cost"):
        self.resource.spending_limits = {component_type: {"month": amount}}
        self.resource.save()

    def use(self, api_key, days_ago, **amounts):
        day = self.today - datetime.timedelta(days=days_ago)
        for component_type, amount in amounts.items():
            models.ResourceApiKeyDailyUsage.objects.create(
                api_key=api_key,
                component_type=component_type,
                date=day,
                usage=Decimal(str(amount)),
            )

    def get(self, user=None, **params):
        self.client.force_authenticate(user or self.fixture.owner)
        return self.client.get(URL, params)

    def chart(self, response, title):
        return next(c for c in response.data["charts"] if c["title"] == title)

    def assertChartsValid(self, response):
        for spec in response.data["charts"]:
            serializer = ChartSpecSerializer(data=json.loads(json.dumps(spec)))
            self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_a_scope_is_required(self):
        response = self.get()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_month_figures_come_from_the_priced_components(self):
        self.use(self.thesis, 0, token_cost=2, input_tokens=1000)
        self.use(self.thesis, 3, token_cost=4)
        self.use(self.batch, 1, token_cost=1.5)
        response = self.get(resource_uuid=self.resource.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertChartsValid(response)
        self.assertEqual(Decimal(response.data["spent_this_month"]), Decimal("7.50"))
        # The last 7 days: 7.50 / 7, rounded up.
        self.assertEqual(Decimal(response.data["daily_pace"]), Decimal("1.08"))
        self.assertEqual(response.data["active_keys"], 1)
        self.assertEqual(response.data["paused_keys"], 1)

    def test_the_forecast_runs_the_recent_pace_to_the_month_end(self):
        self.use(self.thesis, 0, token_cost=7)
        response = self.get(resource_uuid=self.resource.uuid.hex)
        # 7 spent, 1 a day for the 10 days left in November.
        self.assertEqual(
            Decimal(response.data["forecast_this_month"]), Decimal("17.00")
        )

    def test_the_resources_month_limit_and_when_it_is_reached(self):
        self.set_month_limit(12)
        self.use(self.thesis, 0, token_cost=7)
        response = self.get(resource_uuid=self.resource.uuid.hex)
        self.assertChartsValid(response)
        self.assertEqual(Decimal(response.data["month_limit"]), Decimal("12.00"))
        # 5 left at 1 a day.
        self.assertEqual(str(response.data["runs_out"]), "2026-11-25")
        month = self.chart(response, "Spend this month")
        self.assertIn(
            {
                "type": "line_y",
                "value": 12.0,
                "label": "Month limit 12.00 EUR",
                "tone": "danger",
                "value_end": None,
            },
            month["annotations"],
        )

    def test_runs_out_rounds_up_partial_days(self):
        self.set_month_limit(12)
        # 11.50 spent, 7 of it in the last 7 days (pace 1/day): 0.50 left -> tomorrow.
        self.use(self.thesis, 10, token_cost=4.5)
        self.use(self.thesis, 0, token_cost=7)
        response = self.get(resource_uuid=self.resource.uuid.hex)
        self.assertEqual(str(response.data["runs_out"]), "2026-11-21")

    def test_runs_out_is_none_when_forecast_stays_below_limit(self):
        # spent=3.5, pace=0.5/day, 10 days left -> forecast 8.50 < limit 8.90.
        # (8.90 - 3.50) / 0.50 = 10.8 -> ceil is 11 (Dec 1, outside the month).
        self.set_month_limit(8.9)
        self.use(self.thesis, 0, token_cost=3.5)
        response = self.get(resource_uuid=self.resource.uuid.hex)
        self.assertEqual(Decimal(response.data["forecast_this_month"]), Decimal("8.50"))
        self.assertIsNone(response.data["runs_out"])

    def test_hitting_limit_on_month_end_uses_warning_tone(self):
        # spent=7, pace=1, 10 days left -> hits 17 on Nov 30 (month_end).
        self.set_month_limit(17)
        self.use(self.thesis, 0, token_cost=7)
        response = self.get(resource_uuid=self.resource.uuid.hex)
        self.assertEqual(str(response.data["runs_out"]), "2026-11-30")
        month = self.chart(response, "Spend this month")
        forecast = next(s for s in month["series"] if s["name"] == "Forecast")
        self.assertEqual(forecast["tone"], "warning")

    def test_already_over_limit_merges_today_annotation(self):
        self.set_month_limit(5)
        self.use(self.thesis, 0, token_cost=7)
        response = self.get(resource_uuid=self.resource.uuid.hex)
        self.assertChartsValid(response)
        self.assertEqual(str(response.data["runs_out"]), "2026-11-20")
        month = self.chart(response, "Spend this month")
        x_lines = [a for a in month["annotations"] if a["type"] == "line_x"]
        self.assertEqual(len(x_lines), 1)
        self.assertEqual(x_lines[0]["label"], "Today · Limit reached")
        self.assertEqual(x_lines[0]["tone"], "warning")

    def test_no_limit_is_drawn_for_a_project(self):
        self.set_month_limit(12)
        self.use(self.thesis, 0, token_cost=7)
        response = self.get(project_uuid=self.resource.project.uuid.hex)
        self.assertIsNone(response.data["month_limit"])
        self.assertIsNone(response.data["runs_out"])

    def test_no_limit_is_drawn_for_some_of_the_keys(self):
        # One person's keys alone do not tell when the service's limit is hit.
        self.set_month_limit(12)
        self.thesis.user = self.fixture.owner
        self.thesis.save()
        self.use(self.thesis, 0, token_cost=7)
        response = self.get(
            resource_uuid=self.resource.uuid.hex,
            user_uuid=self.fixture.owner.uuid.hex,
        )
        self.assertEqual(Decimal(response.data["spent_this_month"]), Decimal("7.00"))
        self.assertIsNone(response.data["month_limit"])

    def test_no_limit_when_another_priced_usage_component_is_uncapped(self):
        other = factories.OfferingComponentFactory(
            offering=self.offering,
            type="output_cost",
            name="Output cost",
            measured_unit="USD",
            billing_type=BillingTypes.USAGE,
        )
        factories.PlanComponentFactory(
            plan=self.resource.plan, component=other, price=Decimal("1")
        )
        self.set_month_limit(12)
        self.use(self.thesis, 0, output_cost=20)
        response = self.get(resource_uuid=self.resource.uuid.hex)
        self.assertIsNone(response.data["month_limit"])
        self.assertIsNone(response.data["runs_out"])

    def test_spend_per_key_is_stacked_by_key(self):
        self.use(self.thesis, 0, token_cost=2)
        self.use(self.batch, 0, token_cost=1)
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        self.assertChartsValid(response)
        keys = self.chart(response, "Spend per day by key")
        self.assertEqual(keys["chart_type"], "stacked_bar")
        self.assertEqual([s["name"] for s in keys["series"]], ["thesis-runs", "batch"])
        self.assertEqual(len(keys["series"][0]["points"]), 7)
        self.assertEqual(keys["series"][0]["points"][-1][1], 2.0)

    def test_keys_beyond_the_top_ones_are_summed(self):
        for n in range(8):
            self.use(self.make_key(f"cid-x{n}", f"key-{n}"), 0, token_cost=n + 1)
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        self.assertChartsValid(response)
        keys = self.chart(response, "Spend per day by key")
        self.assertEqual(len(keys["series"]), 7)
        self.assertEqual(keys["series"][-1]["name"], "Other keys")
        # key-1 (2) and key-0 (1) did not make the top six.
        self.assertEqual(keys["series"][-1]["points"][-1][1], 3.0)

    def test_duplicate_and_long_key_descriptions_are_disambiguated_and_fitted(self):
        k1 = self.make_key("cid-dup-1", "prod")
        k2 = self.make_key("cid-dup-2", "prod")
        other_named = self.make_key("cid-other-name", "Other keys")
        long_a = self.make_key("cid-long-a", "x" * 120 + "-a")
        long_b = self.make_key("cid-long-b", "x" * 120 + "-b")
        self.use(k1, 0, token_cost=10)
        self.use(k2, 0, token_cost=9)
        self.use(other_named, 0, token_cost=8)
        self.use(long_a, 0, token_cost=7)
        self.use(long_b, 0, token_cost=6)
        for n in range(3):
            self.use(self.make_key(f"cid-extra-{n}", f"extra-{n}"), 0, token_cost=n + 1)
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        self.assertChartsValid(response)
        keys = self.chart(response, "Spend per day by key")
        names = [s["name"] for s in keys["series"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("prod (cid-dup-1)", names)
        self.assertIn("prod (cid-dup-2)", names)
        self.assertIn("Other keys (cid-other-name)", names)
        self.assertIn("Other keys", names)
        self.assertTrue(any(name.endswith("(cid-long-a)") for name in names))
        self.assertTrue(any(name.endswith("(cid-long-b)") for name in names))
        self.assertTrue(all(len(name) <= 100 for name in names))

    def test_zero_usage_rows_do_not_appear_as_spending_keys(self):
        self.use(self.thesis, 0, token_cost=0, input_tokens=0)
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        self.assertChartsValid(response)
        self.assertEqual(self.chart(response, "Spend this month")["series"], [])
        self.assertEqual(self.chart(response, "Spend per day by key")["series"], [])
        self.assertEqual(self.chart(response, "Usage per day")["series"], [])

    def test_sub_cent_stacked_bars_are_not_inflated_by_round_up(self):
        self.cost_plan_component.price = Decimal("0.001")
        self.cost_plan_component.save()
        for n in range(6):
            self.use(self.make_key(f"cid-tiny-{n}", f"tiny-{n}"), 0, token_cost=1)
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        keys = self.chart(response, "Spend per day by key")
        self.assertEqual(keys["series"], [])

    def test_volume_meters_are_charted_per_day(self):
        self.use(self.thesis, 0, input_tokens=1500)
        self.use(self.batch, 0, input_tokens=500)
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        self.assertChartsValid(response)
        usage = self.chart(response, "Usage per day")
        self.assertEqual([s["name"] for s in usage["series"]], ["Input tokens"])
        self.assertEqual(usage["series"][0]["points"][-1][1], 2000.0)
        self.assertEqual(usage["y"]["label"], "tokens")

    def test_volume_chart_caps_series_at_max_series(self):
        for n in range(MAX_SERIES + 4):
            comp = factories.OfferingComponentFactory(
                offering=self.offering,
                type=f"meter_{n:02d}",
                name=f"Meter {n:02d}",
                measured_unit="units",
                billing_type=BillingTypes.USAGE,
            )
            factories.PlanComponentFactory(
                plan=self.resource.plan, component=comp, price=Decimal("0")
            )
            self.use(self.thesis, 0, **{comp.type: n + 1})
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        self.assertChartsValid(response)
        usage = self.chart(response, "Usage per day")
        self.assertEqual(len(usage["series"]), MAX_SERIES)

    def test_volume_chart_does_not_mix_different_units_across_offerings(self):
        other_offering = factories.OfferingFactory(
            plugin_options={"enable_api_key_provisioning": True}
        )
        other_plan = factories.PlanFactory(offering=other_offering)
        other_tokens = factories.OfferingComponentFactory(
            offering=other_offering,
            type="input_tokens",
            name="Input tokens",
            measured_unit="1k tokens",
            billing_type=BillingTypes.USAGE,
        )
        factories.PlanComponentFactory(
            plan=other_plan, component=other_tokens, price=Decimal("0")
        )
        other_resource = factories.ResourceFactory(
            project=self.resource.project,
            offering=other_offering,
            plan=other_plan,
        )
        other_key = self.make_key("cid-other", "other", resource=other_resource)
        self.use(self.thesis, 0, input_tokens=1500)
        self.use(other_key, 0, input_tokens=5)
        response = self.get(project_uuid=self.resource.project.uuid.hex, days=7)
        self.assertChartsValid(response)
        usage = self.chart(response, "Usage per day")
        self.assertEqual(
            [s["name"] for s in usage["series"]],
            ["Input tokens (1k tokens)", "Input tokens (tokens)"],
        )
        self.assertEqual(usage["y"]["label"], "Amount")

    def test_planless_resource_and_removed_component_do_not_leak_into_volume_chart(
        self,
    ):
        self.use(self.thesis, 0, removed_component=50)
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        self.assertChartsValid(response)
        self.assertEqual(self.chart(response, "Usage per day")["series"], [])

        self.resource.plan = None
        self.resource.save()
        self.use(self.thesis, 0, token_cost=10)
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        self.assertChartsValid(response)
        self.assertEqual(Decimal(response.data["spent_this_month"]), Decimal("0"))
        self.assertEqual(self.chart(response, "Usage per day")["series"], [])

    def test_deleted_keys_count_in_spend_but_not_in_active_or_paused(self):
        deleted = self.make_key("cid-del", "old-key", state=States.DELETED)
        self.use(deleted, 0, token_cost=5)
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        self.assertEqual(Decimal(response.data["spent_this_month"]), Decimal("5.00"))
        self.assertEqual(response.data["active_keys"], 1)
        self.assertEqual(response.data["paused_keys"], 1)

    def test_unmanaged_offering_reports_zero_spend_and_no_limit(self):
        self.offering.plugin_options = {"enable_api_key_provisioning": False}
        self.offering.save()
        self.set_month_limit(12)
        self.thesis.usage_period = self.today.replace(day=1)
        self.thesis.estimated_cost = Decimal("10")
        self.thesis.save()
        self.use(self.thesis, 0, token_cost=3, input_tokens=500)
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        self.assertEqual(Decimal(response.data["spent_this_month"]), Decimal("0"))
        self.assertEqual(Decimal(response.data["daily_pace"]), Decimal("0"))
        self.assertIsNone(response.data["month_limit"])
        self.assertEqual(self.chart(response, "Spend this month")["series"], [])
        self.assertEqual(self.chart(response, "Usage per day")["series"], [])

    def test_a_key_reported_only_monthly_still_counts_this_month(self):
        self.make_key(
            "cid-3",
            "monthly",
            current_usages={"token_cost": 4},
            usage_period=self.today.replace(day=1),
            estimated_cost=Decimal("4"),
        )
        response = self.get(resource_uuid=self.resource.uuid.hex)
        self.assertEqual(Decimal(response.data["spent_this_month"]), Decimal("4.00"))

    def test_a_month_total_beyond_the_daily_rows_counts_too(self):
        # The provider reported 10 for the month but only 3 of it by day.
        self.thesis.usage_period = self.today.replace(day=1)
        self.thesis.estimated_cost = Decimal("10")
        self.thesis.save()
        self.use(self.thesis, 0, token_cost=3)
        response = self.get(resource_uuid=self.resource.uuid.hex)
        self.assertEqual(Decimal(response.data["spent_this_month"]), Decimal("10.00"))
        spent = self.chart(response, "Spend this month")["series"][0]["points"]
        self.assertEqual(spent[0][1], 7.0)
        self.assertEqual(spent[-1][1], 10.0)

    def test_price_drop_does_not_fabricate_unreported_spend(self):
        # Daily rows match current_usages, but stored estimated_cost was from an older higher price.
        self.thesis.usage_period = self.today.replace(day=1)
        self.thesis.current_usages = {"token_cost": 10}
        self.thesis.estimated_cost = Decimal("20")
        self.thesis.save()
        self.use(self.thesis, 0, token_cost=10)
        response = self.get(resource_uuid=self.resource.uuid.hex)
        self.assertEqual(Decimal(response.data["spent_this_month"]), Decimal("10.00"))
        month = self.chart(response, "Spend this month")
        self.assertNotIn("not by day", month["subtitle"])

    @freeze_time("2026-11-03 12:00:00")
    def test_daily_pace_keeps_full_seven_day_window_when_days_is_small_early_in_month(
        self,
    ):
        self.today = core_utils.utc_today()
        # Oct 29 (5 days ago) + Nov 3 (today): 7 total over last 7 days -> pace 1.00.
        self.use(self.thesis, 5, token_cost=5)
        self.use(self.thesis, 0, token_cost=2)
        small = self.get(resource_uuid=self.resource.uuid.hex, days=1)
        wide = self.get(resource_uuid=self.resource.uuid.hex, days=30)
        self.assertEqual(Decimal(small.data["daily_pace"]), Decimal("1.00"))
        self.assertEqual(small.data["daily_pace"], wide.data["daily_pace"])
        self.assertEqual(
            small.data["forecast_this_month"], wide.data["forecast_this_month"]
        )

    @freeze_time("2026-11-30 12:00:00")
    def test_last_day_of_month_omits_forecast_series_and_subtitle(self):
        self.today = core_utils.utc_today()
        self.use(self.thesis, 0, token_cost=7)
        response = self.get(resource_uuid=self.resource.uuid.hex)
        month = self.chart(response, "Spend this month")
        self.assertEqual([s["name"] for s in month["series"]], ["Spent"])
        self.assertNotIn("at the recent pace", month["subtitle"])

    @override_settings(TIME_ZONE="America/Chicago")
    @freeze_time("2026-12-01 02:00:00")
    def test_uses_utc_dates_regardless_of_server_timezone(self):
        # 2026-12-01 02:00 UTC is 2026-11-30 20:00 in America/Chicago.
        self.today = core_utils.utc_today()
        self.assertEqual(self.today, datetime.date(2026, 12, 1))
        self.use(self.thesis, 0, token_cost=4)
        self.make_key(
            "cid-utc-monthly",
            "monthly",
            current_usages={"token_cost": 3},
            usage_period=datetime.date(2026, 12, 1),
            estimated_cost=Decimal("3"),
        )
        response = self.get(resource_uuid=self.resource.uuid.hex, days=7)
        self.assertEqual(Decimal(response.data["spent_this_month"]), Decimal("7.00"))
        expected_ms = int(
            datetime.datetime(2026, 12, 1, tzinfo=datetime.UTC).timestamp() * 1000
        )
        spent = self.chart(response, "Spend this month")["series"][0]["points"]
        self.assertEqual(spent[0][0], expected_ms)

    def test_query_count_is_constant_with_key_and_resource_count(self):
        self.use(self.thesis, 0, token_cost=2, input_tokens=100)
        # Warm up lazy fixture properties and Constance cache before measuring.
        self.assertEqual(
            self.get(customer_uuid=self.fixture.customer.uuid.hex, days=7).status_code,
            status.HTTP_200_OK,
        )
        with CaptureQueriesContext(connection) as baseline:
            response = self.get(customer_uuid=self.fixture.customer.uuid.hex, days=7)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for r in range(3):
            extra_resource = factories.ResourceFactory(
                project=self.resource.project,
                offering=self.offering,
                plan=self.resource.plan,
            )
            for k in range(5):
                key = self.make_key(
                    f"cid-perf-{r}-{k}", f"perf-{r}-{k}", resource=extra_resource
                )
                self.use(key, 0, token_cost=1, input_tokens=50)

        with CaptureQueriesContext(connection) as scaled:
            response = self.get(customer_uuid=self.fixture.customer.uuid.hex, days=7)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(scaled), len(baseline))

    def test_provider_customer_and_offering_scopes(self):
        self.use(self.thesis, 0, token_cost=6)
        provider_user = structure_factories.UserFactory()
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.LIST_RESOURCES)
        self.offering.customer.add_user(provider_user, ServiceProviderRole.MANAGER)

        for user, params in (
            (self.fixture.owner, {"customer_uuid": self.fixture.customer.uuid.hex}),
            (self.fixture.owner, {"offering_uuid": self.offering.uuid.hex}),
            (provider_user, {"provider_uuid": self.offering.customer.uuid.hex}),
        ):
            response = self.get(user, **params)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(
                Decimal(response.data["spent_this_month"]), Decimal("6.00")
            )

        # A consumer who is not a provider gets zeros when asking by provider_uuid.
        response = self.get(
            self.fixture.owner, provider_uuid=self.offering.customer.uuid.hex
        )
        self.assertEqual(Decimal(response.data["spent_this_month"]), Decimal("0"))

    def test_an_outsider_sees_no_keys(self):
        self.use(self.thesis, 0, token_cost=2)
        outsider = structure_factories.UserFactory()
        response = self.get(outsider, resource_uuid=self.resource.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data["spent_this_month"]), Decimal("0"))
        self.assertEqual(response.data["active_keys"], 0)

    def test_days_is_bounded(self):
        for bad in (0, -1, 400):
            response = self.get(resource_uuid=self.resource.uuid.hex, days=bad)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CostOfTest(test.APITestCase):
    def test_cost_of_prices_only_the_priced_components(self):
        fixture = MarketplaceFixture()
        resource = fixture.resource
        cost = factories.OfferingComponentFactory(
            offering=resource.offering, type="token_cost"
        )
        factories.PlanComponentFactory(
            plan=resource.plan, component=cost, price=Decimal("2")
        )
        self.assertEqual(
            api_key_cost.cost_of(resource, {"token_cost": 3, "unknown": 9}),
            Decimal(6),
        )
