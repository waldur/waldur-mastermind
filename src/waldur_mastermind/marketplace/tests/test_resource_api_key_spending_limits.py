"""Spending limits of resource API keys per period: day, week, month, quarter, year."""

import datetime
from importlib import import_module
from unittest import mock

from django.apps import apps as django_apps
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_mastermind.marketplace import models, resource_api_keys, serializers, tasks
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_resource_api_key_management import (
    MESSAGES,
    PREPARE,
    PUBLISH,
    Actions,
    ApiKeyManagementTestBase,
    States,
    detail_url,
    list_url,
)

migration = import_module(
    "waldur_mastermind.marketplace.migrations.0315_resource_api_key_monthly_limits"
)
daily_usage_migration = import_module(
    "waldur_mastermind.marketplace.migrations.0316_api_key_daily_usage_option"
)

# A Wednesday; its week started on Monday 19 October.
TODAY = datetime.date(2026, 10, 21)


def utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.UTC)


@freeze_time("2026-10-21 12:00")
class KeySpendingLimitsTestBase(ApiKeyManagementTestBase):
    def setUp(self):
        super().setUp()
        # The agent reports each key's usage per day, so keys are offered the
        # component's periods.
        self.offering.plugin_options["api_key_daily_usage"] = True
        self.offering.save()
        self.cost_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="token_cost",
            billing_type=BillingTypes.USAGE,
            spending_limit_periods={"day": {}, "week": {}, "month": {}, "quarter": {}},
        )
        self.sibling = self.make_key("cid-2")

    def patch(self, data, user=None):
        self.client.force_authenticate(user or self.fixture.owner)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            response = self.client.patch(detail_url(self.key), data, format="json")
        return response, prepare

    def report(self, key=None, **data):
        return self.post(
            self.fixture.offering_owner,
            detail_url(key or self.key, "report-usage"),
            data,
        )

    def day(self, date, amount, component_type="token_cost"):
        return {"type": component_type, "date": date.isoformat(), "amount": amount}

    def set_limits(self, **periods):
        self.key.spending_limits = {"token_cost": periods}
        self.key.save()


class LimitsValidationTest(KeySpendingLimitsTestBase):
    def test_a_key_takes_a_limit_per_offered_period(self):
        response, prepare = self.patch(
            {"spending_limits": {"token_cost": {"day": 10, "week": 40, "month": 100}}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.key.refresh_from_db()
        self.assertEqual(
            self.key.spending_limits,
            {"token_cost": {"day": 10, "week": 40, "month": 100}},
        )
        self.assertEqual(
            response.data["spending_limits"],
            {"token_cost": {"day": 10, "week": 40, "month": 100}},
        )
        self.assertEqual(response.data["limits"], {"token_cost": 100})

    def test_month_is_available_on_every_component(self):
        response, _ = self.patch({"spending_limits": {"tokens": {"month": 5}}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_zero_limit_is_rejected(self):
        # In the flat limits shape zero means no limit; a key is stopped with
        # a pause instead.
        response, prepare = self.patch({"spending_limits": {"tokens": {"month": 0}}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        prepare.assert_not_called()

    def test_periods_the_component_does_not_offer_are_rejected(self):
        for value in (
            {"tokens": {"day": 5}},
            {"token_cost": {"year": 500}},
            {"token_cost": {"hour": 1}},
            {"gpu": {"month": 1}},
        ):
            with self.subTest(value):
                response, prepare = self.patch({"spending_limits": value})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("spending_limits", response.data)
                prepare.assert_not_called()

    def test_a_limit_may_not_exceed_the_resources_limit_for_the_period(self):
        self.resource.spending_limits = {"token_cost": {"day": 20, "month": 300}}
        self.resource.save()
        response, _ = self.patch({"spending_limits": {"token_cost": {"day": 21}}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response, _ = self.patch(
            {"spending_limits": {"token_cost": {"day": 20, "week": 300}}}
        )
        # No resource limit on the week: its month limit caps it.
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_limit_may_not_exceed_the_resources_limit_for_a_longer_period(self):
        self.resource.spending_limits = {"token_cost": {"month": 40}}
        self.resource.save()
        for limits in ({"day": 50}, {"week": 40.01}, {"day": 5, "week": 41}):
            with self.subTest(limits):
                response, prepare = self.patch(
                    {"spending_limits": {"token_cost": limits}}
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(
                    "resource's month spending limit of 40",
                    str(response.data["spending_limits"]),
                )
                prepare.assert_not_called()
        response, _ = self.patch(
            {"spending_limits": {"token_cost": {"day": 40, "week": 40}}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_lowest_of_the_longer_limits_caps(self):
        # Set directly: a quarter below the month is what the cap must read
        # even if the resource's own ordering rule would not let it be set.
        self.resource.spending_limits = {"token_cost": {"month": 300, "quarter": 90}}
        self.resource.save()
        response, _ = self.patch({"spending_limits": {"token_cost": {"day": 91}}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("quarter spending limit of 90", str(response.data))

    def test_a_held_limit_stays_above_a_lowered_longer_cap(self):
        self.set_limits(day=50, month=60)
        self.resource.spending_limits = {"token_cost": {"month": 40}}
        self.resource.save()
        response, _ = self.patch(
            {"spending_limits": {"token_cost": {"day": 50, "month": 40}}}
        )
        # The held day is kept; the ordering rule against the new month
        # still applies to it.
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response, _ = self.patch(
            {"spending_limits": {"token_cost": {"day": 50, "month": 60}}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_providers_maximum_is_not_a_key_cap(self):
        # The maximum bounds what the resource may set; the resource's own
        # limits are what caps a key.
        self.cost_component.spending_limit_periods = {
            "day": {"maximum": 1000},
            "week": {},
            "month": {},
        }
        self.cost_component.save()
        response, _ = self.patch({"spending_limits": {"token_cost": {"day": 1001}}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_shorter_period_may_not_exceed_a_longer_one(self):
        response, _ = self.patch(
            {"spending_limits": {"token_cost": {"day": 50, "week": 40}}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_resources_limit_also_caps_a_new_key(self):
        self.resource.spending_limits = {"token_cost": {"day": 20}}
        self.resource.save()
        response, _ = self.post(
            self.fixture.owner,
            list_url(),
            {
                "resource": self.resource.uuid.hex,
                "spending_limits": {"token_cost": {"day": 30}},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_null_amount_clears_its_period(self):
        self.set_limits(day=10, month=100)
        response, _ = self.patch(
            {"spending_limits": {"token_cost": {"day": None, "month": 100}}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.key.refresh_from_db()
        self.assertEqual(self.key.spending_limits, {"token_cost": {"month": 100}})

    def test_a_held_limit_with_more_decimals_may_be_sent_back(self):
        # The flat monthly limits took any number before they had periods.
        self.key.spending_limits = {"tokens": {"month": 0.125}}
        self.key.save()
        for data in (
            {"spending_limits": {"tokens": {"month": 0.125}}},
            {"limits": {"tokens": 0.125}},
        ):
            with self.subTest(data):
                response, _ = self.patch(data)
                self.assertEqual(
                    response.status_code, status.HTTP_200_OK, response.data
                )
                self.key.refresh_from_db()
                self.assertEqual(self.key.spending_limits, {"tokens": {"month": 0.125}})
        response, _ = self.patch({"spending_limits": {"tokens": {"month": 0.126}}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_held_limit_may_stay_when_its_period_is_no_longer_offered(self):
        self.key.spending_limits = {"tokens": {"day": 5, "month": 50}}
        self.key.save()
        response, _ = self.patch(
            {"spending_limits": {"tokens": {"day": 5, "month": 60}}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response, _ = self.patch(
            {"spending_limits": {"tokens": {"day": 6, "month": 60}}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_monthly_limits_replace_only_the_month(self):
        self.set_limits(day=10, month=100)
        response, prepare = self.patch({"limits": {"token_cost": 200, "tokens": 0}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.key.refresh_from_db()
        self.assertEqual(
            self.key.spending_limits, {"token_cost": {"day": 10, "month": 200}}
        )

    def test_monthly_limits_are_rounded_up_to_the_cent(self):
        # The flat shape took any number before key limits had periods.
        response, _ = self.patch({"limits": {"tokens": 12.345, "token_cost": 0.004}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.key.refresh_from_db()
        self.assertEqual(
            self.key.spending_limits,
            {"tokens": {"month": 12.35}, "token_cost": {"month": 0.01}},
        )

    def test_spending_limits_still_refuse_more_than_two_decimals(self):
        response, _ = self.patch({"spending_limits": {"tokens": {"month": 12.345}}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_monthly_limits_follow_the_ordering_rule(self):
        self.set_limits(day=10, month=100)
        response, _ = self.patch({"limits": {"token_cost": 5}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("limits", response.data)

    def test_limits_and_spending_limits_are_not_taken_together(self):
        response, _ = self.patch(
            {"limits": {"tokens": 5}, "spending_limits": {"tokens": {"month": 5}}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_a_user_who_manages_resource_users_changes_limits(self):
        response, prepare = self.patch(
            {"spending_limits": {"token_cost": {"day": 10}}}, user=self.fixture.member
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertIsNone(self.key.spending_limits)

    def test_a_project_role_with_the_permission_may(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.MANAGE_RESOURCE_USERS)
        response, _ = self.patch(
            {"spending_limits": {"token_cost": {"day": 10}}}, user=self.fixture.manager
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_command_carries_every_period_and_the_month_alone(self):
        response, prepare = self.patch(
            {"spending_limits": {"token_cost": {"day": 10, "month": 100}}}
        )
        payload = self.published_payload(prepare)
        self.assertEqual(payload["action"], Actions.UPDATE)
        self.assertEqual(
            payload["spending_limits"], {"token_cost": {"day": 10, "month": 100}}
        )
        self.assertEqual(payload["limits"], {"token_cost": 100})

    def test_a_key_without_a_month_limit_sends_no_monthly_limits(self):
        _, prepare = self.patch({"spending_limits": {"token_cost": {"day": 10}}})
        self.assertIsNone(self.published_payload(prepare)["limits"])


class ConcurrentEditTest(KeySpendingLimitsTestBase):
    """Limits are validated again with the resource and the key locked.

    The request is validated unlocked, so another edit may commit between
    that and the command.
    """

    def concurrently(self, write):
        original = resource_api_keys.update_settings

        def racing(api_key, actor, changes):
            write()
            return original(api_key, actor, changes)

        return mock.patch.object(resource_api_keys, "update_settings", racing)

    def pause_by_hand(self):
        # The paused branch writes without a command, so no busy guard
        # serializes it.
        models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
            state=States.PAUSED, pending_action=""
        )

    def test_a_held_value_sent_back_after_it_was_lowered_is_refused(self):
        self.set_limits(month=100)
        self.pause_by_hand()
        self.resource.spending_limits = {"token_cost": {"month": 50}}
        self.resource.save()

        def lower_key():
            models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
                spending_limits={"token_cost": {"month": 40}}
            )

        with self.concurrently(lower_key):
            response, _ = self.patch(
                {"spending_limits": {"token_cost": {"day": 5, "month": 100}}}
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("month spending limit of 50", str(response.data))
        self.key.refresh_from_db()
        self.assertEqual(self.key.spending_limits, {"token_cost": {"month": 40}})

    def test_a_cap_lowered_meanwhile_is_applied(self):
        self.resource.spending_limits = {"token_cost": {"month": 200}}
        self.resource.save()

        def lower_cap():
            models.Resource.objects.filter(pk=self.resource.pk).update(
                spending_limits={"token_cost": {"month": 50}}
            )

        with self.concurrently(lower_cap):
            response, prepare = self.patch(
                {"spending_limits": {"token_cost": {"month": 150}}}
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertIsNone(self.key.spending_limits)

    def test_monthly_limits_keep_the_periods_the_key_holds_at_the_lock(self):
        self.set_limits(day=5, month=100)
        self.pause_by_hand()

        def edit_day():
            models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
                spending_limits={"token_cost": {"day": 7, "month": 100}}
            )

        with self.concurrently(edit_day):
            response, _ = self.patch({"limits": {"token_cost": 90}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.key.refresh_from_db()
        self.assertEqual(
            self.key.spending_limits, {"token_cost": {"day": 7, "month": 90}}
        )

    def test_a_new_key_is_checked_against_the_locked_resource(self):
        self.resource.spending_limits = {"token_cost": {"month": 200}}
        self.resource.save()
        original = resource_api_keys.request_key

        def racing(resource, actor, **settings):
            models.Resource.objects.filter(pk=self.resource.pk).update(
                spending_limits={"token_cost": {"month": 50}}
            )
            return original(resource, actor, **settings)

        with mock.patch.object(resource_api_keys, "request_key", racing):
            response, prepare = self.post(
                self.fixture.owner,
                list_url(),
                {
                    "resource": self.resource.uuid.hex,
                    "spending_limits": {"token_cost": {"month": 150}},
                },
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("spending_limits", response.data)
        prepare.assert_not_called()
        self.assertEqual(self.resource.api_keys.count(), 2)


class DailyUsageReportTest(KeySpendingLimitsTestBase):
    def test_usage_per_day_is_stored_and_sums_to_the_month(self):
        response, _ = self.report(
            daily_usages=[
                self.day(TODAY - datetime.timedelta(days=1), "4.50"),
                self.day(TODAY, 3),
            ]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"token_cost": 7.5})
        self.assertEqual(self.key.usage_period, TODAY.replace(day=1))
        self.assertEqual(self.key.daily_usages.count(), 2)

    def test_a_day_reported_again_replaces_its_amount(self):
        self.report(daily_usages=[self.day(TODAY, 3)])
        self.report(daily_usages=[self.day(TODAY, 5)])
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"token_cost": 5})
        self.assertEqual(self.key.daily_usages.get().usage, 5)

    def test_a_day_corrected_downwards_counts_for_its_day_at_once(self):
        self.set_limits(day=4, month=100)
        self.report(daily_usages=[self.day(TODAY, 3)])
        self.report(daily_usages=[self.day(TODAY, 2)])
        self.key.refresh_from_db()
        self.assertEqual(self.key.daily_usages.get().usage, 2)
        # The month figure keeps what was reported until the month turns.
        self.assertEqual(self.key.current_usages, {"token_cost": 3})

    def test_a_monthly_report_keeps_working_beside_days_of_another_component(self):
        self.report(daily_usages=[self.day(TODAY, 3)])
        response, _ = self.report(usages={"tokens": 12})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"token_cost": 3, "tokens": 12})

    def test_a_month_total_for_a_component_with_days_is_rejected(self):
        self.report(daily_usages=[self.day(TODAY, 3)])
        response, _ = self.report(usages={"token_cost": 50})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response, _ = self.report(
            usages={"tokens": 1}, daily_usages=[self.day(TODAY, 3, "tokens")]
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"token_cost": 3})

    def test_a_month_total_before_the_first_days_stays_a_floor(self):
        # An agent switching to daily reporting mid-month must report every
        # day of the month so far; until its days catch up, the month total it
        # reported before stays as a floor and the limit it reached holds.
        self.set_limits(month=1000)
        self.report(usages={"token_cost": 1000})
        self.key.refresh_from_db()
        self.key.set_paused()
        self.key.save()
        _, prepare = self.report(daily_usages=[self.day(TODAY, 20)])
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"token_cost": 1000})
        self.assertEqual(self.key.state, States.PAUSED)

    def test_the_month_figure_follows_the_days_once_they_pass_the_floor(self):
        self.report(usages={"token_cost": 30})
        self.report(
            daily_usages=[
                self.day(TODAY - datetime.timedelta(days=1), 25),
                self.day(TODAY, 10),
            ]
        )
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"token_cost": 35})

    def test_days_of_an_earlier_month_leave_the_month_figure_alone(self):
        self.report(usages={"token_cost": 50})
        response, _ = self.report(
            daily_usages=[self.day(datetime.date(2026, 9, 30), 8)]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"token_cost": 50})
        self.assertEqual(self.key.usage_period, TODAY.replace(day=1))

    def test_days_of_a_later_month_start_it_afresh(self):
        self.report(usages={"tokens": 50}, billing_period="2026-09-01")
        self.report(daily_usages=[self.day(TODAY, 2)])
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"token_cost": 2})
        self.assertEqual(self.key.usage_period, TODAY.replace(day=1))

    def test_bad_daily_reports_are_rejected(self):
        for data in (
            {"daily_usages": [self.day(TODAY + datetime.timedelta(days=1), 1)]},
            {"daily_usages": [self.day(TODAY, 1), self.day(TODAY, 2)]},
            {"daily_usages": [self.day(TODAY, 1, "gpu")]},
            {"daily_usages": [self.day(TODAY, -1)]},
            {},
        ):
            with self.subTest(data):
                response, _ = self.report(**data)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.ResourceApiKeyDailyUsage.objects.exists())

    def test_days_that_count_for_no_limit_are_rejected(self):
        for date in (datetime.date(1970, 1, 1), datetime.date(2025, 12, 31)):
            with self.subTest(date):
                response, _ = self.report(daily_usages=[self.day(date, 1)])
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("counts for no limit", str(response.data))
        self.assertFalse(models.ResourceApiKeyDailyUsage.objects.exists())
        response, _ = self.report(daily_usages=[self.day(datetime.date(2026, 1, 1), 1)])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_last_days_of_the_year_may_be_sent_just_after_it(self):
        with freeze_time("2027-01-01 00:10"):
            response, _ = self.report(
                daily_usages=[
                    self.day(datetime.date(2026, 12, 31), 3),
                    self.day(datetime.date(2027, 1, 1), 1),
                ]
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            models.ResourceApiKeyDailyUsage.objects.filter(api_key=self.key).count(), 2
        )

    def test_a_report_holds_a_bounded_number_of_days(self):
        days = [self.day(TODAY, 1)] * (serializers.MAX_DAILY_USAGE_ITEMS + 1)
        response, _ = self.report(daily_usages=days)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("daily_usages", response.data)
        self.assertFalse(models.ResourceApiKeyDailyUsage.objects.exists())

    def test_the_days_of_a_report_are_written_at_once(self):
        start = datetime.date(2026, 1, 1)
        few = [self.day(start, 1)]
        many = [self.day(start + datetime.timedelta(days=i), 1) for i in range(250)]
        # The first request of a test pays for what it caches.
        self.report(daily_usages=few)
        counts = []
        for days in (few, many):
            with CaptureQueriesContext(connection) as queries:
                response, _ = self.report(daily_usages=days)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            counts.append(len(queries))
        self.assertEqual(counts[0], counts[1])
        self.assertEqual(
            models.ResourceApiKeyDailyUsage.objects.filter(api_key=self.key).count(),
            250,
        )

    def test_an_empty_month_report_still_starts_a_new_month(self):
        self.report(usages={"tokens": 50}, billing_period="2026-09-01")
        response, _ = self.report(usages={})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {})
        self.assertEqual(self.key.usage_period, TODAY.replace(day=1))


class EnforcementTest(KeySpendingLimitsTestBase):
    def test_reaching_a_daily_limit_pauses_that_key_alone_and_says_which(self):
        self.set_limits(day=10, month=100)
        with mock.patch("waldur_mastermind.marketplace.log.event_logger.emit") as emit:
            response, prepare = self.report(daily_usages=[self.day(TODAY, 10)])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.PAUSE)
        self.assertIn("day limit of token_cost", emit.call_args.args[0])
        self.key.refresh_from_db()
        self.assertEqual(self.key.pending_action, Actions.PAUSE)
        self.assertTrue(self.key.paused_by_limit)
        self.assertEqual(self.key.limit_resumes_at, utc(2026, 10, 22))
        self.sibling.refresh_from_db()
        self.assertEqual(self.sibling.state, States.OK)

    def test_a_week_limit_counts_the_days_of_the_week_across_reports(self):
        self.set_limits(week=30)
        self.report(daily_usages=[self.day(datetime.date(2026, 10, 18), 25)])
        _, prepare = self.report(
            daily_usages=[self.day(datetime.date(2026, 10, 19), 15)]
        )
        # Sunday the 18th belongs to the week before.
        prepare.assert_not_called()
        _, prepare = self.report(daily_usages=[self.day(TODAY, 15)])
        self.assertEqual(self.published_payload(prepare)["action"], Actions.PAUSE)
        self.key.refresh_from_db()
        self.assertEqual(self.key.limit_resumes_at, utc(2026, 10, 26))

    def test_a_week_spans_the_month_boundary(self):
        self.set_limits(week=30)
        with freeze_time("2026-10-01 12:00"):
            self.report(daily_usages=[self.day(datetime.date(2026, 9, 29), 20)])
            _, prepare = self.report(
                daily_usages=[self.day(datetime.date(2026, 10, 1), 10)]
            )
        self.assertEqual(self.published_payload(prepare)["action"], Actions.PAUSE)

    def test_a_quarter_adds_the_month_to_earlier_days(self):
        self.set_limits(quarter=100)
        self.report(daily_usages=[self.day(datetime.date(2026, 10, 2), 30)])
        with freeze_time("2026-11-03 12:00"):
            _, prepare = self.report(usages={"tokens": 1})
            prepare.assert_not_called()
            _, prepare = self.report(
                daily_usages=[self.day(datetime.date(2026, 11, 2), 70)]
            )
        self.assertEqual(self.published_payload(prepare)["action"], Actions.PAUSE)

    def test_a_monthly_report_enforces_month_limits_only(self):
        # A key that held a day limit before the provider stopped declaring
        # daily usage; its month limit still applies.
        self.offering.plugin_options["api_key_daily_usage"] = False
        self.offering.save()
        self.set_limits(day=10, month=100)
        _, prepare = self.report(usages={"tokens": 1, "token_cost": 50})
        prepare.assert_not_called()
        _, prepare = self.report(usages={"token_cost": 100})
        self.assertEqual(self.published_payload(prepare)["action"], Actions.PAUSE)
        self.key.refresh_from_db()
        self.assertEqual(self.key.limit_resumes_at, utc(2026, 11, 1))

    def test_the_status_shows_used_and_limit_per_period(self):
        self.set_limits(day=10, month=100)
        self.report(daily_usages=[self.day(TODAY, 4)])
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            list_url(), {"resource_uuid": self.resource.uuid.hex}
        )
        row = next(r for r in response.data if r["uuid"] == self.key.uuid.hex)
        self.assertEqual(
            [
                (s["period"], s["used"], s["limit"], s["reached"])
                for s in row["spending_limit_status"]
            ],
            [("day", 4.0, 10.0, False), ("month", 4.0, 100.0, False)],
        )
        self.assertEqual(
            row["spending_limit_status"][0]["resets_at"], utc(2026, 10, 22)
        )

    def test_listing_keys_reads_their_days_in_one_query(self):
        self.set_limits(day=10)
        self.sibling.spending_limits = {"token_cost": {"day": 10}}
        self.sibling.save()
        self.report(daily_usages=[self.day(TODAY, 4)])
        self.report(self.sibling, daily_usages=[self.day(TODAY, 4)])
        self.client.force_authenticate(self.fixture.owner)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                list_url(), {"resource_uuid": self.resource.uuid.hex}
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        daily = [
            query
            for query in queries.captured_queries
            if "resourceapikeydailyusage" in query["sql"]
        ]
        self.assertEqual(len(daily), 1)


class ResumeTest(KeySpendingLimitsTestBase):
    def pause_for_day_limit(self):
        self.set_limits(day=10, month=100)
        self.report(daily_usages=[self.day(TODAY, 10)])
        self.key.refresh_from_db()
        self.key.set_paused()
        self.key.save()

    def test_a_key_paused_by_a_daily_limit_resumes_after_midnight(self):
        self.pause_for_day_limit()
        with mock.patch(PREPARE, return_value=MESSAGES) as prepare, mock.patch(PUBLISH):
            tasks.resume_api_keys_after_limit_reset()
            prepare.assert_not_called()
            with freeze_time("2026-10-22 00:04"):
                tasks.resume_api_keys_after_limit_reset()
        self.assertEqual(prepare.call_args.args[1]["action"], Actions.RESUME)
        self.key.refresh_from_db()
        self.assertEqual(self.key.pending_action, Actions.RESUME)
        self.assertFalse(self.key.paused_by_limit)
        self.assertIsNone(self.key.limit_resumes_at)

    def test_a_key_paused_by_hand_is_not_resumed(self):
        self.pause_for_day_limit()
        self.key.paused_by_limit = False
        self.key.save()
        with (
            freeze_time("2026-10-22 00:04"),
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            tasks.resume_api_keys_after_limit_reset()
            tasks.resume_api_keys_under_limit()
        prepare.assert_not_called()

    def test_a_longer_limit_reached_since_moves_the_resume_time(self):
        self.pause_for_day_limit()
        self.set_limits(day=10, week=10, month=100)
        self.report(daily_usages=[self.day(TODAY, 10)])
        self.key.refresh_from_db()
        self.assertEqual(self.key.limit_resumes_at, utc(2026, 10, 26))
        with (
            freeze_time("2026-10-22 00:04"),
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            tasks.resume_api_keys_after_limit_reset()
        prepare.assert_not_called()

    def test_a_check_that_finds_the_key_still_over_moves_the_resume_time(self):
        self.pause_for_day_limit()
        # The week limit was added while the key was paused, without a report.
        models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
            spending_limits={"token_cost": {"day": 10, "week": 10}}
        )
        with (
            freeze_time("2026-10-22 00:04"),
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            tasks.resume_api_keys_after_limit_reset()
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.limit_resumes_at, utc(2026, 10, 26))

    def test_raising_the_limit_resumes_the_key(self):
        self.pause_for_day_limit()
        response, prepare = self.patch(
            {"spending_limits": {"token_cost": {"day": 20, "month": 100}}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["action"], Actions.RESUME)
        self.assertEqual(payload["spending_limits"]["token_cost"]["day"], 20)

    def test_the_resume_time_is_shown(self):
        self.pause_for_day_limit()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(detail_url(self.key))
        self.assertTrue(response.data["paused_by_limit"])
        self.assertEqual(response.data["limit_resumes_at"], utc(2026, 10, 22))


# pytest may run with --no-migrations, which empties MIGRATION_MODULES and with
# it the loader; the historical registry is built from the migration files.
@override_settings(MIGRATION_MODULES={})
class MonthlyLimitsMigrationTest(test.APITestCase):
    def test_existing_limits_become_month_limits_and_zero_none(self):
        resource = factories.ResourceFactory()
        limited = models.ResourceApiKey.objects.create(
            resource=resource,
            client_id="cid-1",
            spending_limits={"tokens": 100, "images": 0.5, "gpu": 0},
        )
        unlimited = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-2", spending_limits={"gpu": 0}
        )
        hand_edited = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-3", spending_limits={"gpu": True}
        )
        apps = MigrationLoader(None).project_state().apps
        migration.limits_to_months(apps, None)

        limited.refresh_from_db()
        self.assertEqual(
            limited.spending_limits,
            {"tokens": {"month": 100}, "images": {"month": 0.5}},
        )
        self.assertEqual(limited.monthly_limits, {"tokens": 100, "images": 0.5})
        unlimited.refresh_from_db()
        self.assertIsNone(unlimited.spending_limits)
        hand_edited.refresh_from_db()
        self.assertIsNone(hand_edited.spending_limits)

    def test_the_way_back_keeps_month_limits(self):
        key = models.ResourceApiKey.objects.create(
            resource=factories.ResourceFactory(),
            client_id="cid-1",
            spending_limits={"tokens": {"day": 5, "month": 100}, "gpu": {"day": 1}},
        )
        apps = MigrationLoader(None).project_state().apps
        migration.months_to_limits(apps, None)
        key.refresh_from_db()
        self.assertEqual(key.spending_limits, {"tokens": 100})


class DailyUsageOptionMigrationTest(test.APITestCase):
    def test_an_offering_whose_keys_have_days_declares_daily_usage(self):
        reporting = factories.ResourceFactory()
        reporting.offering.plugin_options = {"enable_api_key_provisioning": True}
        reporting.offering.save()
        key = models.ResourceApiKey.objects.create(resource=reporting, client_id="c1")
        models.ResourceApiKeyDailyUsage.objects.create(
            api_key=key, component_type="tokens", date=TODAY, usage=1
        )
        monthly = factories.ResourceFactory()
        models.ResourceApiKey.objects.create(resource=monthly, client_id="c2")
        declined = factories.ResourceFactory()
        declined.offering.plugin_options = {"api_key_daily_usage": False}
        declined.offering.save()
        declined_key = models.ResourceApiKey.objects.create(
            resource=declined, client_id="c3"
        )
        models.ResourceApiKeyDailyUsage.objects.create(
            api_key=declined_key, component_type="tokens", date=TODAY, usage=1
        )

        # The models as they are: a migration state is not built under
        # --no-migrations.
        daily_usage_migration.declare_daily_usage(django_apps, None)

        reporting.offering.refresh_from_db()
        self.assertEqual(
            reporting.offering.plugin_options,
            {"enable_api_key_provisioning": True, "api_key_daily_usage": True},
        )
        monthly.offering.refresh_from_db()
        self.assertNotIn("api_key_daily_usage", monthly.offering.plugin_options)
        declined.offering.refresh_from_db()
        self.assertIs(declined.offering.plugin_options["api_key_daily_usage"], False)


@freeze_time("2026-10-21 12:00")
class OfferedPeriodsTest(KeySpendingLimitsTestBase):
    """Which periods a key is offered follows the offering's two options and the plan's prices."""

    def set_options(self, **options):
        self.offering.plugin_options.update(options)
        self.offering.save()

    def test_without_daily_usage_only_month_is_offered(self):
        self.set_options(api_key_daily_usage=False)
        for period in ("day", "week", "quarter"):
            with self.subTest(period):
                response, prepare = self.patch(
                    {"spending_limits": {"token_cost": {period: 5}}}
                )
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
                self.assertIn("per day", str(response.data["spending_limits"]))
                prepare.assert_not_called()
        response, _ = self.patch({"spending_limits": {"token_cost": {"month": 50}}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_daily_usage_is_off_unless_set(self):
        del self.offering.plugin_options["api_key_daily_usage"]
        self.offering.save()
        response, _ = self.patch({"spending_limits": {"token_cost": {"day": 5}}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_with_daily_usage_the_components_periods_are_offered(self):
        response, _ = self.patch(
            {
                "spending_limits": {
                    "token_cost": {"day": 5, "week": 20, "month": 50, "quarter": 90}
                }
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_with_limits_off_no_limit_is_offered(self):
        self.set_options(api_key_limits=False)
        for value in (
            {"spending_limits": {"token_cost": {"month": 5}}},
            {"spending_limits": {"tokens": {"month": 5}}},
            {"limits": {"tokens": 5}},
        ):
            with self.subTest(value):
                response, prepare = self.patch(value)
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
                self.assertIn("does not offer limits", str(response.data))
                prepare.assert_not_called()
        response, _ = self.post(
            self.fixture.owner,
            list_url(),
            {
                "resource": self.resource.uuid.hex,
                "spending_limits": {"token_cost": {"month": 5}},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_limits_are_on_unless_turned_off(self):
        self.assertNotIn("api_key_limits", self.offering.plugin_options)
        response, _ = self.patch({"spending_limits": {"token_cost": {"month": 5}}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_held_limits_stay_when_their_periods_are_withdrawn(self):
        self.set_limits(day=5, month=50)
        self.set_options(api_key_limits=False)
        # Sent back as held, or cleared: both pass.
        response, _ = self.patch(
            {"spending_limits": {"token_cost": {"day": 5, "month": 50}}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response, _ = self.patch({"spending_limits": {"token_cost": {"day": 6}}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response, _ = self.patch({"spending_limits": None})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_held_day_limit_stays_without_daily_usage(self):
        self.set_limits(day=5, month=50)
        self.set_options(api_key_daily_usage=False)
        response, _ = self.patch(
            {"spending_limits": {"token_cost": {"day": 5, "month": 60}}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_component_the_plan_prices_at_zero_takes_no_limit(self):
        plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(
            plan=plan, component=self.cost_component, price=2
        )
        tokens = self.offering.components.get(type="tokens")
        factories.PlanComponentFactory(plan=plan, component=tokens, price=0)
        self.resource.plan = plan
        self.resource.save()
        response, prepare = self.patch({"spending_limits": {"tokens": {"month": 5}}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not charged", str(response.data))
        prepare.assert_not_called()
        response, _ = self.patch({"spending_limits": {"token_cost": {"day": 5}}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_held_limit_on_a_free_component_may_stay(self):
        self.key.spending_limits = {"tokens": {"month": 5}}
        self.key.save()
        plan = factories.PlanFactory(offering=self.offering)
        tokens = self.offering.components.get(type="tokens")
        factories.PlanComponentFactory(plan=plan, component=tokens, price=0)
        self.resource.plan = plan
        self.resource.save()
        response, _ = self.patch(
            {"spending_limits": {"tokens": {"month": 5}, "token_cost": {"day": 1}}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_component_the_plan_does_not_price_still_takes_limits(self):
        self.resource.plan = factories.PlanFactory(offering=self.offering)
        self.resource.save()
        response, _ = self.patch({"spending_limits": {"tokens": {"month": 5}}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


@freeze_time("2026-10-21 12:00")
class ComponentRenameTest(KeySpendingLimitsTestBase):
    def test_a_renamed_component_takes_its_key_limits_and_usage_along(self):
        self.set_limits(day=10, month=100)
        self.report(daily_usages=[self.day(TODAY, 4)])
        modified = models.ResourceApiKey.objects.get(pk=self.key.pk).modified
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.OfferingFactory.get_url(
                self.offering, "update_offering_component"
            ),
            {"uuid": self.cost_component.uuid.hex, "type": "inference_cost"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.key.refresh_from_db()
        self.assertEqual(
            self.key.spending_limits, {"inference_cost": {"day": 10, "month": 100}}
        )
        self.assertEqual(self.key.current_usages, {"inference_cost": 4})
        self.assertEqual(self.key.daily_usages.get().component_type, "inference_cost")
        self.assertEqual(self.key.modified, modified)
