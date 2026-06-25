"""Tests for the period-boundary gating of `reset_raw_usage`.

The site-agent treats `reset_raw_usage=True` as a sacctmgr
`modify account … set RawUsage=0` command — wiping the account's
accumulated SLURM usage. If the mastermind sync task emits that flag on
every 10-min cycle (rather than only when a new period has started), the
SLURM accumulator never grows past one cycle and GrpTRESMins ceases to be
an enforceable budget.

These tests pin the desired behavior:

* first sync within a period → `reset_raw_usage=True`;
* every subsequent sync within the **same** period → `reset_raw_usage=False`;
* first sync after a period rollover → `reset_raw_usage=True` again.

They fail against the current implementation (which copies
``policy.raw_usage_reset`` verbatim into every emitted message) and
pass once the gate in ``calculate_slurm_settings`` /
``apply_policy_actions`` is in place.
"""

from __future__ import annotations

import json
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from freezegun import freeze_time

from waldur_core.logging.tests import factories as logging_factories
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING, LimitPeriods
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import models as policy_models


def _extract_settings(mock_publish) -> list[dict]:
    """Return one ``settings`` dict per publish_messages call."""
    extracted = []
    for call in mock_publish.call_args_list:
        messages = call.args[0]
        for message in messages:
            payload = json.loads(message["payload"])
            extracted.append(payload["settings"])
    return extracted


class RawUsageResetPeriodGatingTest(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)

        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = marketplace_factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            project=self.fixture.offering_project,
            customer=self.fixture.offering_customer,
        )
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="node",
            billing_type=marketplace_models.BillingTypes.LIMIT,
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        marketplace_factories.PlanComponentFactory(
            plan=self.plan, component=self.component, amount=1000
        )
        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="acct-rusage",
            limits={"node": 1000},
        )

        self.policy = policy_models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners",
            apply_to_all=True,
            grace_ratio=0.3,
            carryover_enabled=False,
            limit_type="GrpTRESMins",
            tres_billing_enabled=False,
            raw_usage_reset=True,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )

        # The agent only receives messages when there is a subscription
        # queue for the offering; the policy code short-circuits otherwise.
        event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.fixture.offering_owner,
            observable_objects=[{"object_type": "resource_periodic_limits"}],
        )
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=event_subscription,
            offering_uuid=self.offering.uuid,
            object_type="resource_periodic_limits",
        )

    @freeze_time("2026-05-19 11:00:00")
    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_first_sync_in_period_sends_reset(self, mock_publish) -> None:
        success = self.policy.apply_policy_actions(self.resource)
        self.assertTrue(success)

        settings_list = _extract_settings(mock_publish)
        self.assertEqual(len(settings_list), 1)
        self.assertTrue(
            settings_list[0]["reset_raw_usage"],
            "First sync of the period must request RawUsage=0 — that's the "
            "whole point of the period rollover.",
        )

    @freeze_time("2026-05-19 11:00:00")
    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_subsequent_syncs_within_same_period_do_not_reset(
        self, mock_publish
    ) -> None:
        # Two consecutive syncs at 11:00 and 11:10, both inside 2026-05.
        self.policy.apply_policy_actions(self.resource)
        self.policy.apply_policy_actions(self.resource)

        settings_list = _extract_settings(mock_publish)
        self.assertEqual(len(settings_list), 2)
        self.assertTrue(settings_list[0]["reset_raw_usage"])
        self.assertFalse(
            settings_list[1]["reset_raw_usage"],
            "Second sync inside the same period must NOT wipe RawUsage — "
            "doing so on every 10-min cycle effectively gives users "
            "unlimited compute (the production bug).",
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_reset_fires_again_after_period_rollover(self, mock_publish) -> None:
        with freeze_time("2026-05-19 11:00:00"):
            self.policy.apply_policy_actions(self.resource)
            self.policy.apply_policy_actions(self.resource)

        with freeze_time("2026-06-01 00:05:00"):
            self.policy.apply_policy_actions(self.resource)

        settings_list = _extract_settings(mock_publish)
        self.assertEqual(len(settings_list), 3)
        self.assertEqual(
            [s["reset_raw_usage"] for s in settings_list],
            [True, False, True],
            "Expected reset on May 19 (first sync of month), no reset on "
            "the second May 19 sync, then reset again on June 1 (new month).",
        )

    @freeze_time("2026-05-19 11:00:00")
    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_policy_with_raw_usage_reset_disabled_never_resets(
        self, mock_publish
    ) -> None:
        """Sanity: the gate must not invent True for a policy where the
        operator has explicitly disabled raw usage resets."""
        self.policy.raw_usage_reset = False
        self.policy.save()

        self.policy.apply_policy_actions(self.resource)
        self.policy.apply_policy_actions(self.resource)

        settings_list = _extract_settings(mock_publish)
        self.assertEqual(len(settings_list), 2)
        self.assertFalse(settings_list[0]["reset_raw_usage"])
        self.assertFalse(settings_list[1]["reset_raw_usage"])


class RawUsageResetSingleShotTest(TestCase):
    """Single-shot (`-s`) offerings must never emit `reset_raw_usage`.

    A single-shot offering uses a TOTAL ``limit_period``: the budget spans
    the whole active life of the resource, so there is no period boundary at
    which a reset makes sense. Wiping ``RawUsage=0`` there discards the very
    usage the limit is meant to cap.

    The period-boundary gate alone is not enough: ``current_period`` is the
    constant ``"total"``, so the first sync (``last_synced_period`` is None)
    and every cache-marker eviction would still emit one reset. These tests
    pin that single-shot resources stay at ``reset_raw_usage=False`` on every
    sync — including the first. Regression test for HPCMP-495.
    """

    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)

        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = marketplace_factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            project=self.fixture.offering_project,
            customer=self.fixture.offering_customer,
        )
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="node",
            billing_type=marketplace_models.BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        marketplace_factories.PlanComponentFactory(
            plan=self.plan, component=self.component, amount=1000
        )
        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            backend_id="acct-singleshot",
            limits={"node": 1000},
        )

        self.policy = policy_models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners",
            apply_to_all=True,
            grace_ratio=0.3,
            carryover_enabled=False,
            limit_type="GrpTRESMins",
            tres_billing_enabled=False,
            raw_usage_reset=True,
            period=invoices_models.PeriodMixin.Periods.TOTAL,
        )

        event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.fixture.offering_owner,
            observable_objects=[{"object_type": "resource_periodic_limits"}],
        )
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=event_subscription,
            offering_uuid=self.offering.uuid,
            object_type="resource_periodic_limits",
        )

    @freeze_time("2026-05-19 11:00:00")
    def test_current_period_is_total(self) -> None:
        # Guards the precondition: the gate keys off the "total" period.
        self.assertEqual(self.policy._get_current_period(), "total")

    @freeze_time("2026-05-19 11:00:00")
    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_first_sync_does_not_reset_for_single_shot(self, mock_publish) -> None:
        self.policy.apply_policy_actions(self.resource)

        settings_list = _extract_settings(mock_publish)
        self.assertEqual(len(settings_list), 1)
        self.assertFalse(
            settings_list[0]["reset_raw_usage"],
            "Single-shot (-s) offerings must never request RawUsage=0 — not "
            "even on the first sync (HPCMP-495).",
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_single_shot_never_resets_across_cache_eviction(self, mock_publish) -> None:
        with freeze_time("2026-05-19 11:00:00"):
            self.policy.apply_policy_actions(self.resource)

        # Simulate the synced-period marker being evicted (90-day TTL or a
        # cache flush) — the period-boundary gate would re-fire here, but the
        # single-shot guard must keep it suppressed.
        cache.clear()

        with freeze_time("2026-08-25 11:00:00"):
            self.policy.apply_policy_actions(self.resource)

        settings_list = _extract_settings(mock_publish)
        self.assertEqual(len(settings_list), 2)
        self.assertEqual(
            [s["reset_raw_usage"] for s in settings_list],
            [False, False],
            "A cache eviction must not cause a single-shot offering to reset.",
        )
