"""Tests for the anonymous-marketplace-assistant models.

Each model's required behaviour is captured here as the gating contract.
Implementation in chat/anonymous/models.py follows the same shapes; tests
are the first line of defense for the model invariants the spec calls out.
"""

import threading
from datetime import timedelta

from constance.test import override_config
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_mastermind.chat.anonymous.models import (
    AnonymousChatBudget,
    AnonymousChatFeedback,
    AnonymousChatInteraction,
    SessionBinding,
)
from waldur_mastermind.chat.budget_gate import (
    CapacityException,
    enforce_global_budget,
)
from waldur_mastermind.chat.models import GlobalAssistantBudget


class AnonymousChatBudgetForIPTest(TestCase):
    """Mirrors TokenQuota.for_user idiom: get-or-create with optional row lock."""

    def test_for_ip_creates_row_on_first_access(self):
        self.assertFalse(
            AnonymousChatBudget.objects.filter(ip_address="1.2.3.4").exists()
        )
        budget = AnonymousChatBudget.for_ip("1.2.3.4")
        self.assertTrue(
            AnonymousChatBudget.objects.filter(ip_address="1.2.3.4").exists()
        )
        self.assertEqual(budget.daily_token_usage, 0)
        self.assertEqual(budget.daily_injection_strikes, 0)

    def test_for_ip_returns_existing_row(self):
        first = AnonymousChatBudget.for_ip("1.2.3.4")
        first.daily_token_usage = 100
        first.save()
        second = AnonymousChatBudget.for_ip("1.2.3.4")
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.daily_token_usage, 100)

    def test_for_ip_lock_works_inside_atomic(self):
        with transaction.atomic():
            budget = AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            self.assertEqual(budget.ip_address, "1.2.3.4")


# Pin to a Wednesday mid-month, mid-day. Avoids day/week/month boundary
# flakes when the suite happens to run within the first few minutes/hours
# of midnight, Monday, or the 1st (the previous "X ago is always within
# the current period" assumption broke at month boundaries — see HPCMP-484
# pipeline run on 2026-06-01 00:05 UTC).
@freeze_time("2024-04-17 12:00:00")
class AnonymousChatBudgetLazyResetTest(TestCase):
    def test_resets_when_reset_predates_today(self):
        with transaction.atomic():
            budget = AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            AnonymousChatBudget.objects.filter(pk=budget.pk).update(
                daily_token_usage=999,
                daily_injection_strikes=3,
                # Yesterday: comfortably before any midnight today.
                daily_reset_last_at=timezone.now() - timedelta(days=2),
            )
            budget.refresh_from_db()
            budget.ensure_period_reset()
            self.assertEqual(budget.daily_token_usage, 0)
            self.assertEqual(budget.daily_injection_strikes, 0)

    def test_no_reset_when_reset_after_today_start(self):
        with transaction.atomic():
            budget = AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            AnonymousChatBudget.objects.filter(pk=budget.pk).update(
                daily_token_usage=500,
                # 5 minutes ago — clearly within today.
                daily_reset_last_at=timezone.now() - timedelta(minutes=5),
            )
            budget.refresh_from_db()
            budget.ensure_period_reset()
            self.assertEqual(budget.daily_token_usage, 500)

    def test_weekly_resets_when_reset_last_at_before_this_monday(self):
        with transaction.atomic():
            budget = AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            AnonymousChatBudget.objects.filter(pk=budget.pk).update(
                weekly_token_usage=888,
                # 10 days ago — comfortably before any Monday boundary.
                weekly_reset_last_at=timezone.now() - timedelta(days=10),
            )
            budget.refresh_from_db()
            budget.ensure_period_reset()
            self.assertEqual(budget.weekly_token_usage, 0)

    def test_weekly_does_not_reset_when_reset_within_this_week(self):
        with transaction.atomic():
            budget = AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            # 1 hour ago is always within the current week.
            AnonymousChatBudget.objects.filter(pk=budget.pk).update(
                weekly_token_usage=300,
                weekly_reset_last_at=timezone.now() - timedelta(hours=1),
            )
            budget.refresh_from_db()
            budget.ensure_period_reset()
            self.assertEqual(budget.weekly_token_usage, 300)

    def test_monthly_resets_when_reset_last_at_before_first_of_month(self):
        with transaction.atomic():
            budget = AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            AnonymousChatBudget.objects.filter(pk=budget.pk).update(
                monthly_token_usage=777,
                # 40 days ago — guaranteed to be before the 1st of the current month.
                monthly_reset_last_at=timezone.now() - timedelta(days=40),
            )
            budget.refresh_from_db()
            budget.ensure_period_reset()
            self.assertEqual(budget.monthly_token_usage, 0)

    def test_monthly_does_not_reset_when_reset_within_this_month(self):
        with transaction.atomic():
            budget = AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            AnonymousChatBudget.objects.filter(pk=budget.pk).update(
                monthly_token_usage=400,
                # 6 hours ago is always within the current month.
                monthly_reset_last_at=timezone.now() - timedelta(hours=6),
            )
            budget.refresh_from_db()
            budget.ensure_period_reset()
            self.assertEqual(budget.monthly_token_usage, 400)


class AnonymousChatBudgetAddUsageTest(TestCase):
    def test_add_usage_increments_atomically(self):
        with transaction.atomic():
            budget = AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            budget.add_usage(tokens=100)
            budget.add_usage(tokens=250)
        budget.refresh_from_db()
        self.assertEqual(budget.daily_token_usage, 350)

    def test_add_usage_rejects_negative(self):
        with transaction.atomic():
            budget = AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            with self.assertRaises(ValueError):
                budget.add_usage(tokens=-1)

    def test_add_usage_increments_all_three_windows_atomically(self):
        with transaction.atomic():
            budget = AnonymousChatBudget.for_ip("1.2.3.4", lock=True)
            budget.add_usage(tokens=150)
        budget.refresh_from_db()
        self.assertEqual(budget.daily_token_usage, 150)
        self.assertEqual(budget.weekly_token_usage, 150)
        self.assertEqual(budget.monthly_token_usage, 150)


class AnonymousChatBudgetIsExhaustedTest(TestCase):
    """Tests for is_period_exhausted across daily / weekly / monthly."""

    @override_config(AI_ASSISTANT_TOKEN_LIMIT_DAILY=100000)
    def test_daily_not_exhausted_when_below_cap(self):
        budget = AnonymousChatBudget.for_ip("1.2.3.4")
        budget.daily_token_usage = 99999
        self.assertFalse(budget.is_period_exhausted("daily"))

    @override_config(AI_ASSISTANT_TOKEN_LIMIT_DAILY=100000)
    def test_daily_exhausted_when_at_cap(self):
        budget = AnonymousChatBudget.for_ip("1.2.3.4")
        budget.daily_token_usage = 100000
        self.assertTrue(budget.is_period_exhausted("daily"))

    @override_config(AI_ASSISTANT_TOKEN_LIMIT_DAILY=-1)
    def test_daily_not_exhausted_when_unlimited(self):
        budget = AnonymousChatBudget.for_ip("1.2.3.4")
        budget.daily_token_usage = 10_000_000
        self.assertFalse(budget.is_period_exhausted("daily"))

    @override_config(AI_ASSISTANT_TOKEN_LIMIT_WEEKLY=50000)
    def test_weekly_exhausted_when_at_cap(self):
        budget = AnonymousChatBudget.for_ip("1.2.3.4")
        budget.weekly_token_usage = 50000
        self.assertTrue(budget.is_period_exhausted("weekly"))

    @override_config(AI_ASSISTANT_TOKEN_LIMIT_WEEKLY=-1)
    def test_weekly_not_exhausted_when_unlimited(self):
        budget = AnonymousChatBudget.for_ip("1.2.3.4")
        budget.weekly_token_usage = 10_000_000
        self.assertFalse(budget.is_period_exhausted("weekly"))

    @override_config(AI_ASSISTANT_TOKEN_LIMIT_MONTHLY=200000)
    def test_monthly_exhausted_when_at_cap(self):
        budget = AnonymousChatBudget.for_ip("1.2.3.4")
        budget.monthly_token_usage = 200000
        self.assertTrue(budget.is_period_exhausted("monthly"))

    @override_config(AI_ASSISTANT_TOKEN_LIMIT_MONTHLY=-1)
    def test_monthly_not_exhausted_when_unlimited(self):
        budget = AnonymousChatBudget.for_ip("1.2.3.4")
        budget.monthly_token_usage = 10_000_000
        self.assertFalse(budget.is_period_exhausted("monthly"))

    @override_config(AI_ASSISTANT_TOKEN_LIMIT_DAILY=100000)
    def test_strike_throttle_applies_to_daily_cap(self):
        # Strike threshold (5) reached → effective cap is base // 10.
        budget = AnonymousChatBudget.for_ip("1.2.3.4")
        budget.daily_injection_strikes = 5  # at threshold
        budget.daily_token_usage = 10000  # exactly 100000 // 10
        self.assertTrue(budget.is_period_exhausted("daily"))

    @override_config(AI_ASSISTANT_TOKEN_LIMIT_DAILY=100000)
    def test_strike_throttle_does_not_apply_below_threshold(self):
        budget = AnonymousChatBudget.for_ip("1.2.3.4")
        budget.daily_injection_strikes = 4  # below threshold
        budget.daily_token_usage = 10000  # less than full 100000 cap
        self.assertFalse(budget.is_period_exhausted("daily"))


class GlobalAssistantBudgetTest(TestCase):
    def test_get_returns_singleton(self):
        first = GlobalAssistantBudget.get()
        second = GlobalAssistantBudget.get()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(GlobalAssistantBudget.objects.count(), 1)

    def test_add_usage_increments_daily_only(self):
        # ``minute_request_usage`` is bumped at admission (by
        # ``enforce_global_budget``), not on completion — see
        # GlobalAssistantBudgetEnforceMinuteCounterTest.
        with transaction.atomic():
            budget = GlobalAssistantBudget.get(lock=True)
            budget.add_usage(tokens=500)
        budget.refresh_from_db()
        self.assertEqual(budget.daily_token_usage, 500)
        self.assertEqual(budget.minute_request_usage, 0)

    def test_add_usage_resets_period_first(self):
        # Auth path calls add_usage without an explicit reset; the call must
        # roll over the period itself or tokens written across midnight UTC
        # land on a stale row that the next gate read zeroes (silent leak).
        with transaction.atomic():
            budget = GlobalAssistantBudget.get(lock=True)
            GlobalAssistantBudget.objects.filter(pk=budget.pk).update(
                daily_token_usage=999,
                daily_reset_last_at=timezone.now() - timedelta(days=2),
            )
            budget.refresh_from_db()
            budget.add_usage(tokens=10)
        budget.refresh_from_db()
        # 999 belonged to a previous day — must not survive the rollover.
        self.assertEqual(budget.daily_token_usage, 10)


class GlobalAssistantBudgetEnforceMinuteCounterTest(TestCase):
    """Burst cap counts admissions, not completions — verified at the gate."""

    def test_gate_increments_minute_counter_on_admission(self):
        with transaction.atomic():
            enforce_global_budget()
        with transaction.atomic():
            enforce_global_budget()

        budget = GlobalAssistantBudget.get()
        self.assertEqual(budget.minute_request_usage, 2)

    @override_config(AI_ASSISTANT_GLOBAL_REQUESTS_PER_MINUTE=1)
    def test_gate_does_not_increment_when_minute_cap_hit(self):
        with transaction.atomic():
            budget = GlobalAssistantBudget.get(lock=True)
            GlobalAssistantBudget.objects.filter(pk=budget.pk).update(
                minute_request_usage=1,
                minute_reset_last_at=timezone.now(),
            )

        with self.assertRaises(CapacityException):
            with transaction.atomic():
                enforce_global_budget()

        # Rejected request must not push the counter further past the cap.
        budget = GlobalAssistantBudget.get()
        self.assertEqual(budget.minute_request_usage, 1)


class GlobalAssistantBudgetMinuteResetTest(TestCase):
    def test_minute_counter_resets_at_minute_boundary(self):
        with transaction.atomic():
            budget = GlobalAssistantBudget.get(lock=True)
            GlobalAssistantBudget.objects.filter(pk=budget.pk).update(
                minute_request_usage=42,
                minute_reset_last_at=timezone.now() - timedelta(minutes=2),
            )
            budget.refresh_from_db()
            budget.ensure_period_reset()
        self.assertEqual(budget.minute_request_usage, 0)

    def test_minute_counter_persists_within_same_minute(self):
        with transaction.atomic():
            budget = GlobalAssistantBudget.get(lock=True)
            GlobalAssistantBudget.objects.filter(pk=budget.pk).update(
                minute_request_usage=42,
                minute_reset_last_at=timezone.now(),
            )
            budget.refresh_from_db()
            budget.ensure_period_reset()
        self.assertEqual(budget.minute_request_usage, 42)


class GlobalAssistantBudgetPeriodResetTest(TestCase):
    def test_reset_writes_period_boundary_not_now(self):
        # ``*_reset_last_at`` must point at the bucket start, not the request
        # time, so admin/reporting can read "when did this period start".
        before = timezone.now()
        day_start = before.replace(hour=0, minute=0, second=0, microsecond=0)
        with transaction.atomic():
            budget = GlobalAssistantBudget.get(lock=True)
            GlobalAssistantBudget.objects.filter(pk=budget.pk).update(
                daily_token_usage=100,
                daily_reset_last_at=before - timedelta(days=2),
            )
            budget.refresh_from_db()
            budget.ensure_period_reset()
        self.assertEqual(budget.daily_reset_last_at, day_start)


class AnonymousChatFeedbackTest(TestCase):
    def setUp(self):
        self.interaction = AnonymousChatInteraction.objects.create()

    def test_can_have_only_human_signal(self):
        fb = AnonymousChatFeedback.objects.create(
            interaction=self.interaction,
            score=1,
            submitted_at=timezone.now(),
        )
        self.assertEqual(fb.score, 1)
        self.assertIsNone(fb.llm_resolution_score)
        self.assertIsNone(fb.llm_reviewed_at)

    def test_can_have_only_llm_signal(self):
        fb = AnonymousChatFeedback.objects.create(
            interaction=self.interaction,
            llm_resolution_score=4,
            llm_intent_category="compute",
            llm_reviewed_at=timezone.now(),
            llm_judge_model="some-model",
        )
        self.assertEqual(fb.llm_resolution_score, 4)
        self.assertIsNone(fb.score)

    def test_can_have_both_signals(self):
        now = timezone.now()
        fb = AnonymousChatFeedback.objects.create(
            interaction=self.interaction,
            score=-1,
            comment="not what I wanted",
            category="not_relevant",
            submitted_from_ip="1.2.3.4",
            submitted_at=now,
            llm_resolution_score=2,
            llm_intent_category="compute",
            llm_reviewed_at=now,
            llm_judge_model="qwen3.5",
        )
        self.assertEqual(fb.score, -1)
        self.assertEqual(fb.llm_resolution_score, 2)

    def test_one_to_one_with_interaction(self):
        AnonymousChatFeedback.objects.create(
            interaction=self.interaction, score=1, submitted_at=timezone.now()
        )
        # Second create with the same interaction must fail (PK is the FK).
        with self.assertRaises(Exception):
            AnonymousChatFeedback.objects.create(interaction=self.interaction, score=-1)


class SessionBindingTest(TestCase):
    def test_claim_creates_first_time(self):
        binding = SessionBinding.claim("session-abc", "1.2.3.4")
        self.assertEqual(binding.session_id, "session-abc")
        self.assertEqual(binding.ip_address, "1.2.3.4")

    def test_claim_idempotent_for_same_session(self):
        first = SessionBinding.claim("session-abc", "1.2.3.4")
        second = SessionBinding.claim("session-abc", "1.2.3.4")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SessionBinding.objects.count(), 1)

    def test_claim_does_not_overwrite_ip(self):
        # If the session was created from IP A and IP B claims, claim()
        # MUST NOT update the IP — the binding is immutable. The view
        # layer enforces 403 on mismatch.
        SessionBinding.claim("session-abc", "1.2.3.4")
        SessionBinding.claim("session-abc", "9.9.9.9")
        binding = SessionBinding.objects.get(session_id="session-abc")
        self.assertEqual(binding.ip_address, "1.2.3.4")

    def test_claim_touches_last_seen(self):
        first = SessionBinding.claim("session-abc", "1.2.3.4")
        original_last_seen = first.last_seen
        # Force a measurable time gap
        SessionBinding.objects.filter(pk=first.pk).update(
            last_seen=timezone.now() - timedelta(hours=2)
        )
        SessionBinding.claim("session-abc", "1.2.3.4")
        binding = SessionBinding.objects.get(session_id="session-abc")
        self.assertGreater(
            binding.last_seen, original_last_seen - timedelta(hours=2, seconds=1)
        )


class SessionBindingRaceTest(TransactionTestCase):
    """Use TransactionTestCase so threads see committed data — DB-level race."""

    def test_concurrent_claim_results_in_one_binding(self):
        # Two threads racing to claim the same session_id from different IPs.
        # Exactly one binding should exist afterwards.
        bindings: list[SessionBinding] = []
        errors: list[Exception] = []

        def worker(ip):
            try:
                bindings.append(SessionBinding.claim("race-session", ip))
            except Exception as e:
                errors.append(e)
            finally:
                connection.close()

        t1 = threading.Thread(target=worker, args=("1.2.3.4",))
        t2 = threading.Thread(target=worker, args=("9.9.9.9",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # No exceptions — get_or_create absorbs the race.
        self.assertEqual(errors, [])
        # Exactly one binding row.
        self.assertEqual(
            SessionBinding.objects.filter(session_id="race-session").count(), 1
        )
