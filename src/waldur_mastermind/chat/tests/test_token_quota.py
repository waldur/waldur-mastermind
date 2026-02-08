from datetime import datetime, timedelta
from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import TokenQuota
from waldur_mastermind.chat.serializers import TokenQuotaUsageResponseSerializer
from waldur_mastermind.chat.tasks import (
    reset_daily_token_usage,
    reset_monthly_token_usage,
    reset_weekly_token_usage,
)


class TokenQuotaCreationTest(test.APITransactionTestCase):
    """Test TokenQuota lazy creation and initialization."""

    def test_get_or_create_creates_new_quota(self):
        """for_user creates TokenQuota with zero usage."""
        user = structure_factories.UserFactory()
        quota = TokenQuota.for_user(user)

        self.assertIsNotNone(quota)
        self.assertEqual(quota.user, user)
        self.assertEqual(quota.daily_usage, 0)
        self.assertEqual(quota.weekly_usage, 0)
        self.assertEqual(quota.monthly_usage, 0)

    def test_for_user_returns_existing_quota(self):
        """for_user returns existing quota without creating duplicate."""
        user = structure_factories.UserFactory()

        quota1 = TokenQuota.for_user(user)
        quota2 = TokenQuota.for_user(user)

        self.assertEqual(quota1.id, quota2.id)
        self.assertEqual(TokenQuota.objects.filter(user=user).count(), 1)

    def test_concurrent_for_user_with_lock_creates_single_quota(self):
        """Concurrent for_user calls with lock=True create only one quota."""
        import threading

        from django.db import connection

        user = structure_factories.UserFactory()
        results = []
        errors = []

        def create_quota_with_lock():
            try:
                # Close connection to force new connection per thread
                connection.close()
                with transaction.atomic():
                    quota = TokenQuota.for_user(user, lock=True)
                    results.append(quota.id)
            except Exception as e:
                errors.append(e)

        # Launch 5 concurrent threads trying to create quota for same user
        threads = []
        for _ in range(5):
            t = threading.Thread(target=create_quota_with_lock)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Verify only one quota was created
        self.assertEqual(TokenQuota.objects.filter(user=user).count(), 1)
        # All successful results should have same quota ID
        if results:
            self.assertTrue(all(qid == results[0] for qid in results))
        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")


class TokenQuotaResetAtSerializerTest(test.APITestCase):
    """Test that serializer calculates next reset times correctly."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.quota = TokenQuota.for_user(self.user)

    def test_daily_reset_at_is_next_midnight(self):
        """Daily reset_at is calculated as next midnight."""
        now = timezone.make_aware(datetime(2026, 1, 25, 14, 30, 0))
        with mock.patch("django.utils.timezone.now", return_value=now):
            serializer = TokenQuotaUsageResponseSerializer(self.quota)
            reset_at = serializer.data["daily_reset_at"]

        # Should be tomorrow at 00:00:00
        self.assertEqual(reset_at.year, 2026)
        self.assertEqual(reset_at.month, 1)
        self.assertEqual(reset_at.day, 26)
        self.assertEqual(reset_at.hour, 0)
        self.assertEqual(reset_at.minute, 0)

    def test_weekly_reset_at_is_next_monday(self):
        """Weekly reset_at is calculated as next Monday at midnight."""
        # Saturday Jan 25, 2026
        now = timezone.make_aware(datetime(2026, 1, 25, 10, 0, 0))
        with mock.patch("django.utils.timezone.now", return_value=now):
            serializer = TokenQuotaUsageResponseSerializer(self.quota)
            reset_at = serializer.data["weekly_reset_at"]

        # Next Monday is Jan 26, 2026
        self.assertEqual(reset_at.year, 2026)
        self.assertEqual(reset_at.month, 1)
        self.assertEqual(reset_at.day, 26)
        self.assertEqual(reset_at.hour, 0)
        self.assertEqual(reset_at.minute, 0)

    def test_monthly_reset_at_is_first_of_next_month(self):
        """Monthly reset_at is calculated as first day of next month."""
        now = timezone.make_aware(datetime(2026, 1, 25, 10, 0, 0))
        with mock.patch("django.utils.timezone.now", return_value=now):
            serializer = TokenQuotaUsageResponseSerializer(self.quota)
            reset_at = serializer.data["monthly_reset_at"]

        # Should be Feb 1, 2026
        self.assertEqual(reset_at.year, 2026)
        self.assertEqual(reset_at.month, 2)
        self.assertEqual(reset_at.day, 1)
        self.assertEqual(reset_at.hour, 0)
        self.assertEqual(reset_at.minute, 0)

    def test_monthly_reset_at_year_rollover(self):
        """Monthly reset handles December to January transition."""
        now = timezone.make_aware(datetime(2026, 12, 20, 10, 0, 0))
        with mock.patch("django.utils.timezone.now", return_value=now):
            serializer = TokenQuotaUsageResponseSerializer(self.quota)
            reset_at = serializer.data["monthly_reset_at"]

        # Should be Jan 1, 2027
        self.assertEqual(reset_at.year, 2027)
        self.assertEqual(reset_at.month, 1)
        self.assertEqual(reset_at.day, 1)


class TokenQuotaLimitsTest(test.APITestCase):
    """Test effective limit calculation with user overrides and system defaults."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.quota = TokenQuota.for_user(self.user)

    @override_constance_config(
        LLM_TOKEN_LIMIT_DAILY=10000,
        LLM_TOKEN_LIMIT_WEEKLY=50000,
        LLM_TOKEN_LIMIT_MONTHLY=100000,
    )
    def test_uses_system_defaults_when_user_limits_null(self):
        """Effective limits fall back to constance config when user limits are null."""
        self.assertEqual(self.quota.get_effective_limit("daily"), 10000)
        self.assertEqual(self.quota.get_effective_limit("weekly"), 50000)
        self.assertEqual(self.quota.get_effective_limit("monthly"), 100000)

    @override_constance_config(
        LLM_TOKEN_LIMIT_DAILY=10000,
        LLM_TOKEN_LIMIT_WEEKLY=50000,
        LLM_TOKEN_LIMIT_MONTHLY=100000,
    )
    def test_uses_user_limits_when_set(self):
        """User-specific limits override system defaults."""
        self.quota.daily_limit = 5000
        self.quota.weekly_limit = 25000
        self.quota.monthly_limit = 60000
        self.quota.save()

        self.assertEqual(self.quota.get_effective_limit("daily"), 5000)
        self.assertEqual(self.quota.get_effective_limit("weekly"), 25000)
        self.assertEqual(self.quota.get_effective_limit("monthly"), 60000)

    @override_constance_config(
        LLM_TOKEN_LIMIT_DAILY=-1,
        LLM_TOKEN_LIMIT_WEEKLY=-1,
        LLM_TOKEN_LIMIT_MONTHLY=-1,
    )
    def test_unlimited_when_system_default_is_none(self):
        self.assertEqual(-1, self.quota.get_effective_limit("daily"))
        self.assertEqual(-1, self.quota.get_effective_limit("weekly"))
        self.assertEqual(-1, self.quota.get_effective_limit("monthly"))

    @override_constance_config(LLM_TOKEN_LIMIT_DAILY=10000)
    def test_user_can_set_unlimited_with_zero(self):
        """User limit of -1 means unlimited (overrides system default)."""
        self.quota.daily_limit = -1
        self.quota.save()
        self.assertEqual(-1, self.quota.get_effective_limit("daily"))

    def test_invalid_period_raises_error(self):
        """get_effective_limit raises ValueError for invalid period."""
        with self.assertRaises(ValueError):
            self.quota.get_effective_limit("yearly")

    def test_coerce_limit_rejects_invalid_types(self):
        """_coerce_limit raises ValueError for non-integer values."""
        with self.assertRaises(ValueError) as ctx:
            TokenQuota._coerce_limit("invalid", "test_field")
        self.assertIn("must be an integer", str(ctx.exception))
        self.assertIn("test_field", str(ctx.exception))

    def test_coerce_limit_rejects_values_below_negative_one(self):
        """_coerce_limit raises ValueError for values < -1."""
        with self.assertRaises(ValueError) as ctx:
            TokenQuota._coerce_limit(-5, "test_field")
        self.assertIn("must be >= -1", str(ctx.exception))

    def test_coerce_limit_accepts_none(self):
        """_coerce_limit returns None for None input."""
        result = TokenQuota._coerce_limit(None)
        self.assertIsNone(result)

    def test_coerce_limit_accepts_negative_one(self):
        """_coerce_limit accepts -1 (unlimited)."""
        result = TokenQuota._coerce_limit(-1)
        self.assertEqual(result, -1)

    def test_coerce_limit_accepts_zero_and_positive(self):
        """_coerce_limit accepts 0 and positive integers."""
        self.assertEqual(TokenQuota._coerce_limit(0), 0)
        self.assertEqual(TokenQuota._coerce_limit(100), 100)
        self.assertEqual(TokenQuota._coerce_limit(999999), 999999)


class TokenQuotaInvalidConfigTest(test.APITestCase):
    """Test handling of invalid constance configuration values."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.quota = TokenQuota.for_user(self.user)

    @override_constance_config(LLM_TOKEN_LIMIT_DAILY="not_a_number")
    def test_invalid_string_config_raises_error(self):
        """get_effective_limit raises ValueError when system default is invalid string."""
        with self.assertRaises(ValueError) as ctx:
            self.quota.get_effective_limit("daily")
        self.assertIn("must be an integer", str(ctx.exception))
        self.assertIn("LLM_TOKEN_LIMIT_DAILY", str(ctx.exception))

    @override_constance_config(LLM_TOKEN_LIMIT_WEEKLY=-5)
    def test_config_below_negative_one_raises_error(self):
        """get_effective_limit raises ValueError when system default is < -1."""
        with self.assertRaises(ValueError) as ctx:
            self.quota.get_effective_limit("weekly")
        self.assertIn("must be >= -1", str(ctx.exception))

    @override_constance_config(LLM_TOKEN_LIMIT_DAILY=1000)
    def test_user_invalid_limit_raises_error_on_save(self):
        """Setting invalid user limit should be caught by model validation."""
        # Setting limit below -1 should fail validation
        self.quota.daily_limit = -5
        with self.assertRaises(Exception):  # Django ValidationError
            self.quota.full_clean()

    @override_constance_config(LLM_TOKEN_LIMIT_WEEKLY="invalid")
    def test_invalid_config_prevents_quota_check(self):
        """Invalid system config prevents quota validation."""
        # This should raise when trying to check effective limits
        with self.assertRaises(ValueError):
            self.quota.get_effective_limit("weekly")


class CalendarBasedResetTasksTest(test.APITestCase):
    """Test calendar-based reset tasks."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.quota = TokenQuota.for_user(self.user)

    def test_daily_reset_resets_previous_day_usage(self):
        """Daily task resets quotas where last reset was on a previous calendar day."""
        yesterday = timezone.now() - timedelta(days=1)
        self.quota.daily_usage = 5000
        self.quota.daily_reset_last_at = yesterday
        self.quota.save()

        reset_daily_token_usage()

        self.quota.refresh_from_db()
        self.assertEqual(self.quota.daily_usage, 0)
        # Timestamp should be updated to roughly now
        self.assertIsNotNone(self.quota.daily_reset_last_at)
        self.assertGreater(self.quota.daily_reset_last_at, yesterday)

    def test_daily_reset_skips_same_day_usage(self):
        """Daily task does not reset if already reset today."""
        now = timezone.now()
        self.quota.daily_usage = 5000
        self.quota.daily_reset_last_at = now
        self.quota.save()

        reset_daily_token_usage()

        self.quota.refresh_from_db()
        # Should not be reset
        self.assertEqual(self.quota.daily_usage, 5000)

    def test_weekly_reset_resets_previous_week_usage(self):
        """Weekly task resets quotas from previous calendar week."""
        last_week = timezone.now() - timedelta(days=8)
        self.quota.weekly_usage = 20000
        self.quota.weekly_reset_last_at = last_week
        self.quota.save()

        reset_weekly_token_usage()

        self.quota.refresh_from_db()
        self.assertEqual(self.quota.weekly_usage, 0)
        self.assertGreater(self.quota.weekly_reset_last_at, last_week)

    def test_monthly_reset_resets_previous_month_usage(self):
        """Monthly task resets quotas from previous calendar month."""
        last_month = timezone.now() - timedelta(days=32)
        self.quota.monthly_usage = 80000
        self.quota.monthly_reset_last_at = last_month
        self.quota.save()

        reset_monthly_token_usage()

        self.quota.refresh_from_db()
        self.assertEqual(self.quota.monthly_usage, 0)
        self.assertGreater(self.quota.monthly_reset_last_at, last_month)


class TokenQuotaUsageTest(test.APITransactionTestCase):
    """Test usage recording."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.quota = TokenQuota.for_user(self.user)

    def test_add_usage_increments_all_periods(self):
        """add_usage increments daily, weekly, and monthly usage."""
        initial_daily = self.quota.daily_usage
        initial_weekly = self.quota.weekly_usage
        initial_monthly = self.quota.monthly_usage

        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.add_usage(1000)

        self.quota.refresh_from_db()

        self.assertEqual(self.quota.daily_usage, initial_daily + 1000)
        self.assertEqual(self.quota.weekly_usage, initial_weekly + 1000)
        self.assertEqual(self.quota.monthly_usage, initial_monthly + 1000)

    def test_add_usage_multiple_times(self):
        """Multiple add_usage calls accumulate correctly."""
        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.add_usage(500)

        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.add_usage(300)

        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.add_usage(200)

        self.quota.refresh_from_db()
        self.assertEqual(self.quota.daily_usage, 1000)
        self.assertEqual(self.quota.weekly_usage, 1000)
        self.assertEqual(self.quota.monthly_usage, 1000)


class TokenQuotaRemainingTest(test.APITestCase):
    """Test remaining token calculations."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.quota = TokenQuota.for_user(self.user)

    @override_constance_config(LLM_TOKEN_LIMIT_DAILY=10000)
    def test_get_remaining_returns_correct_value(self):
        """get_remaining calculates remaining tokens correctly."""
        self.quota.daily_usage = 3000
        self.quota.save()

        remaining = self.quota.get_remaining("daily")

        self.assertEqual(remaining, 7000)

    @override_constance_config(LLM_TOKEN_LIMIT_MONTHLY=100000)
    def test_get_remaining_returns_zero_when_exhausted(self):
        """get_remaining returns 0 when quota is exhausted."""
        self.quota.monthly_usage = 100000
        self.quota.save()

        remaining = self.quota.get_remaining("monthly")

        self.assertEqual(remaining, 0)

    @override_constance_config(LLM_TOKEN_LIMIT_DAILY=10000)
    def test_get_remaining_never_negative(self):
        """get_remaining returns 0, not negative, when over limit."""
        self.quota.daily_usage = 15000  # Over limit
        self.quota.save()

        remaining = self.quota.get_remaining("daily")

        self.assertEqual(remaining, 0)

    @override_constance_config(LLM_TOKEN_LIMIT_WEEKLY=-1)
    def test_get_remaining_returns_none_when_unlimited(self):
        """get_remaining returns None for unlimited quotas."""
        remaining = self.quota.get_remaining("weekly")

        self.assertIsNone(remaining)


class TokenQuotaConcurrencyTest(test.APITransactionTestCase):
    """Tests for race condition handling in token quota."""

    def test_get_locked_quota_requires_transaction(self):
        """get_locked_quota raises error outside transaction."""
        user = structure_factories.UserFactory()
        with self.assertRaises(RuntimeError):
            TokenQuota.for_user(user, True)

    def test_add_usage_requires_transaction(self):
        """add_usage raises error outside transaction."""
        user = structure_factories.UserFactory()
        quota = TokenQuota.for_user(user)
        with self.assertRaises(RuntimeError):
            quota.add_usage(100)

    def test_add_usage_rejects_negative_tokens(self):
        """add_usage raises ValueError for negative token counts."""
        user = structure_factories.UserFactory()
        quota = TokenQuota.for_user(user)

        with transaction.atomic():
            locked_quota = TokenQuota.objects.select_for_update().get(pk=quota.pk)
            with self.assertRaises(ValueError) as ctx:
                locked_quota.add_usage(-100)
            self.assertIn("non-negative", str(ctx.exception))


class QuotaUsageAPITest(test.APITestCase):
    """Test quota usage API endpoint."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)
        self.usage_url = reverse("chatquota-usage")

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_MONTHLY=1000,
    )
    def test_get_own_usage(self):
        """User can view their own usage."""
        quota = TokenQuota.for_user(self.user)
        quota.monthly_limit = 1000
        quota.monthly_usage = 500
        quota.save()

        response = self.client.get(self.usage_url)
        self.assertEqual(response.data["monthly_limit"], 1000)

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_MONTHLY=1000,
    )
    def test_get_usage_with_user_specific_limit(self):
        """Usage respects user-specific quota limit."""
        quota = TokenQuota.for_user(self.user)
        quota.monthly_limit = 2000
        quota.monthly_usage = 1500
        quota.save()

        response = self.client.get(self.usage_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["monthly_usage"], 1500)
        self.assertEqual(response.data["monthly_limit"], 2000)

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_MONTHLY=-1,
    )
    def test_get_usage_unlimited(self):
        """Usage shows unlimited when limit is None."""
        quota = TokenQuota.for_user(self.user)
        quota.monthly_limit = None  # User's limit is None = use system's unlimited
        quota.monthly_usage = 5000
        quota.save()

        response = self.client.get(self.usage_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["monthly_usage"], 5000)
        self.assertIsNone(response.data["monthly_limit"])

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_MONTHLY=1000,
    )
    def test_staff_can_view_other_user_usage(self):
        """Staff user can view usage for any user."""
        other_user = structure_factories.UserFactory()
        quota = TokenQuota.for_user(other_user)
        quota.monthly_usage = 300
        quota.save()

        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        response = self.client.get(self.usage_url, {"user_uuid": str(other_user.uuid)})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["monthly_usage"], 300)

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
    )
    def test_user_cannot_view_other_user_usage(self):
        """User cannot view usage for other users."""
        other_user = structure_factories.UserFactory()

        response = self.client.get(self.usage_url, {"user_uuid": str(other_user.uuid)})

        self.assertEqual(response.status_code, 403)

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_DAILY=5000,
        LLM_TOKEN_LIMIT_WEEKLY=25000,
        LLM_TOKEN_LIMIT_MONTHLY=100000,
    )
    def test_response_includes_system_defaults(self):
        """Response includes system default limits from constance config."""
        TokenQuota.for_user(self.user)

        response = self.client.get(self.usage_url)

        self.assertEqual(response.status_code, 200)
        # Verify system defaults are included
        self.assertEqual(response.data["daily_system_default"], 5000)
        self.assertEqual(response.data["weekly_system_default"], 25000)
        self.assertEqual(response.data["monthly_system_default"], 100000)

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_DAILY=-1,
        LLM_TOKEN_LIMIT_WEEKLY=-1,
        LLM_TOKEN_LIMIT_MONTHLY=-1,
    )
    def test_system_defaults_show_unlimited(self):
        """System defaults show -1 when configured as unlimited."""
        TokenQuota.for_user(self.user)

        response = self.client.get(self.usage_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["daily_system_default"], -1)
        self.assertEqual(response.data["weekly_system_default"], -1)
        self.assertEqual(response.data["monthly_system_default"], -1)

    @override_constance_config(
        LLM_CHAT_ENABLED=True,
        LLM_INFERENCES_API_URL="https://example.com/stream",
        LLM_INFERENCES_API_TOKEN="dummy-token",
        LLM_TOKEN_LIMIT_DAILY=10000,
        LLM_TOKEN_LIMIT_WEEKLY=50000,
        LLM_TOKEN_LIMIT_MONTHLY=200000,
    )
    def test_system_defaults_visible_even_with_custom_limits(self):
        """System defaults are shown even when user has custom limits."""
        quota = TokenQuota.for_user(self.user)
        quota.daily_limit = 2000
        quota.weekly_limit = 10000
        quota.monthly_limit = 40000
        quota.save()

        response = self.client.get(self.usage_url)

        self.assertEqual(response.status_code, 200)
        # User's custom limits
        self.assertEqual(response.data["daily_limit"], 2000)
        self.assertEqual(response.data["weekly_limit"], 10000)
        self.assertEqual(response.data["monthly_limit"], 40000)
        # System defaults (for transparency)
        self.assertEqual(response.data["daily_system_default"], 10000)
        self.assertEqual(response.data["weekly_system_default"], 50000)
        self.assertEqual(response.data["monthly_system_default"], 200000)


class SetTokenQuotaAPITest(test.APITestCase):
    """Tests for staff setting user AI token quotas via set_quota endpoint."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.target_user = structure_factories.UserFactory()
        self.regular_user = structure_factories.UserFactory()
        self.url = reverse("chatquota-set-quota")

    def test_staff_can_set_monthly_quota(self):
        """Staff user can set monthly quota for any user."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            self.url,
            data={"user_uuid": str(self.target_user.uuid), "monthly_limit": 5000},
        )

        self.assertEqual(response.status_code, 200)
        quota = TokenQuota.objects.get(user=self.target_user)
        self.assertEqual(quota.monthly_limit, 5000)

    def test_support_can_set_monthly_quota(self):
        """Support user can set monthly quota for any user."""
        self.client.force_authenticate(user=self.support)
        response = self.client.post(
            self.url,
            data={"user_uuid": str(self.target_user.uuid), "monthly_limit": 3000},
        )

        self.assertEqual(response.status_code, 200)
        quota = TokenQuota.objects.get(user=self.target_user)
        self.assertEqual(quota.monthly_limit, 3000)

    def test_staff_can_set_all_quota_types(self):
        """Staff can set daily, weekly, and monthly quotas together."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            self.url,
            data={
                "user_uuid": str(self.target_user.uuid),
                "daily_limit": 1000,
                "weekly_limit": 5000,
                "monthly_limit": 20000,
            },
        )

        self.assertEqual(response.status_code, 200)
        quota = TokenQuota.objects.get(user=self.target_user)
        self.assertEqual(quota.daily_limit, 1000)
        self.assertEqual(quota.weekly_limit, 5000)
        self.assertEqual(quota.monthly_limit, 20000)

    def test_non_staff_cannot_set_quota(self):
        """Regular user cannot set quotas."""
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(
            self.url,
            data={"user_uuid": str(self.target_user.uuid), "monthly_limit": 5000},
        )

        self.assertEqual(response.status_code, 403)

    def test_rejects_negative_quota(self):
        """Negative quotas are rejected (except -1 for unlimited)."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            self.url,
            data={"user_uuid": str(self.target_user.uuid), "monthly_limit": -2},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("monthly_limit", response.data)

    def test_set_zero_quota_blocks_all_usage(self):
        """Setting quota to 0 blocks all token usage."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            self.url,
            data={"user_uuid": str(self.target_user.uuid), "monthly_limit": 0},
        )

        self.assertEqual(response.status_code, 200)
        quota = TokenQuota.objects.get(user=self.target_user)
        self.assertEqual(quota.monthly_limit, 0)
        self.assertEqual(quota.get_effective_limit("monthly"), 0)

    def test_set_null_quota_uses_system_default(self):
        """Setting quota to null means use system default."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            self.url,
            data={"user_uuid": str(self.target_user.uuid), "monthly_limit": None},
        )

        self.assertEqual(response.status_code, 200)
        quota = TokenQuota.objects.get(user=self.target_user)
        self.assertIsNone(quota.monthly_limit)

    def test_invalid_user_uuid_returns_404(self):
        """Invalid user UUID returns 404."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            self.url,
            data={
                "user_uuid": "00000000-0000-0000-0000-000000000000",
                "monthly_limit": 5000,
            },
        )

        self.assertEqual(response.status_code, 404)

    def test_missing_user_uuid_returns_400(self):
        """Missing user_uuid field returns 400."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(self.url, data={"monthly_limit": 5000})

        self.assertEqual(response.status_code, 400)

    def test_partial_update_only_specified_fields(self):
        """Only specified quota fields are updated."""
        # Create existing quota with all limits set
        quota = TokenQuota.for_user(self.target_user)
        quota.daily_limit = 1000
        quota.weekly_limit = 5000
        quota.monthly_limit = 20000
        quota.save()

        # Update only monthly limit
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            self.url,
            data={"user_uuid": str(self.target_user.uuid), "monthly_limit": 30000},
        )

        self.assertEqual(response.status_code, 200)
        quota.refresh_from_db()
        # Daily and weekly should remain unchanged
        self.assertEqual(quota.daily_limit, 1000)
        self.assertEqual(quota.weekly_limit, 5000)
        # Monthly should be updated
        self.assertEqual(quota.monthly_limit, 30000)

    def test_user_cannot_set_own_quota(self):
        """User cannot modify their own quota."""
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(
            self.url,
            data={"user_uuid": str(self.regular_user.uuid), "monthly_limit": 999999},
        )

        self.assertEqual(response.status_code, 403)


class LazyResetTest(test.APITransactionTestCase):
    """Test lazy reset functionality for token quotas."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.quota = TokenQuota.for_user(self.user)

    def test_resets_stale_daily_usage(self):
        """ensure_periods_reset resets daily usage if last reset was yesterday."""
        yesterday = timezone.now() - timedelta(days=1)
        self.quota.daily_usage = 5000
        self.quota.daily_reset_last_at = yesterday
        self.quota.save()

        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.ensure_periods_reset()

        self.quota.refresh_from_db()
        self.assertEqual(self.quota.daily_usage, 0)
        self.assertGreater(self.quota.daily_reset_last_at, yesterday)

    def test_resets_stale_weekly_usage(self):
        """ensure_periods_reset resets weekly usage if last reset was last week."""
        last_week = timezone.now() - timedelta(days=8)
        self.quota.weekly_usage = 20000
        self.quota.weekly_reset_last_at = last_week
        self.quota.save()

        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.ensure_periods_reset()

        self.quota.refresh_from_db()
        self.assertEqual(self.quota.weekly_usage, 0)
        self.assertGreater(self.quota.weekly_reset_last_at, last_week)

    def test_resets_stale_monthly_usage(self):
        """ensure_periods_reset resets monthly usage if last reset was last month."""
        last_month = timezone.now() - timedelta(days=32)
        self.quota.monthly_usage = 80000
        self.quota.monthly_reset_last_at = last_month
        self.quota.save()

        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.ensure_periods_reset()

        self.quota.refresh_from_db()
        self.assertEqual(self.quota.monthly_usage, 0)
        self.assertGreater(self.quota.monthly_reset_last_at, last_month)

    def test_resets_multiple_stale_periods_at_once(self):
        """ensure_periods_reset resets all stale periods in a single update."""
        long_ago = timezone.now() - timedelta(days=40)
        self.quota.daily_usage = 1000
        self.quota.weekly_usage = 5000
        self.quota.monthly_usage = 20000
        self.quota.daily_reset_last_at = long_ago
        self.quota.weekly_reset_last_at = long_ago
        self.quota.monthly_reset_last_at = long_ago
        self.quota.save()

        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.ensure_periods_reset()

        self.quota.refresh_from_db()
        # All periods should be reset
        self.assertEqual(self.quota.daily_usage, 0)
        self.assertEqual(self.quota.weekly_usage, 0)
        self.assertEqual(self.quota.monthly_usage, 0)
        # All reset timestamps should be updated
        self.assertGreater(self.quota.daily_reset_last_at, long_ago)
        self.assertGreater(self.quota.weekly_reset_last_at, long_ago)
        self.assertGreater(self.quota.monthly_reset_last_at, long_ago)

    def test_does_not_reset_current_period_usage(self):
        """ensure_periods_reset does not reset usage for current period."""
        now = timezone.now()
        self.quota.daily_usage = 1000
        self.quota.weekly_usage = 5000
        self.quota.monthly_usage = 20000
        self.quota.daily_reset_last_at = now
        self.quota.weekly_reset_last_at = now
        self.quota.monthly_reset_last_at = now
        self.quota.save()

        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.ensure_periods_reset()

        self.quota.refresh_from_db()
        # Usage should remain unchanged
        self.assertEqual(self.quota.daily_usage, 1000)
        self.assertEqual(self.quota.weekly_usage, 5000)
        self.assertEqual(self.quota.monthly_usage, 20000)

    def test_is_idempotent(self):
        """ensure_periods_reset can be called multiple times safely."""
        yesterday = timezone.now() - timedelta(days=1)
        self.quota.daily_usage = 5000
        self.quota.daily_reset_last_at = yesterday
        self.quota.save()

        # First call resets
        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.ensure_periods_reset()

        self.quota.refresh_from_db()
        self.assertEqual(self.quota.daily_usage, 0)
        first_reset_time = self.quota.daily_reset_last_at

        # Second call is a no-op
        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.ensure_periods_reset()

        self.quota.refresh_from_db()
        self.assertEqual(self.quota.daily_usage, 0)
        # Reset time should be unchanged (no update happened)
        self.assertEqual(self.quota.daily_reset_last_at, first_reset_time)

    def test_raises_error_outside_transaction(self):
        """ensure_periods_reset raises RuntimeError outside transaction.atomic()."""
        with self.assertRaises(RuntimeError) as ctx:
            self.quota.ensure_periods_reset()
        self.assertIn("transaction.atomic()", str(ctx.exception))

    def test_works_with_add_usage(self):
        """ensure_periods_reset followed by add_usage starts from 0."""
        yesterday = timezone.now() - timedelta(days=1)
        self.quota.daily_usage = 9000
        self.quota.daily_reset_last_at = yesterday
        self.quota.save()

        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.ensure_periods_reset()
            quota.add_usage(500)

        self.quota.refresh_from_db()
        # Usage should be 500 (reset to 0, then added 500)
        self.assertEqual(self.quota.daily_usage, 500)

    def test_partial_reset_only_stale_periods(self):
        """ensure_periods_reset only resets stale periods, not current ones."""
        # Daily is stale, weekly and monthly are current
        yesterday = timezone.now() - timedelta(days=1)
        now = timezone.now()

        self.quota.daily_usage = 1000
        self.quota.weekly_usage = 5000
        self.quota.monthly_usage = 20000
        self.quota.daily_reset_last_at = yesterday
        self.quota.weekly_reset_last_at = now
        self.quota.monthly_reset_last_at = now
        self.quota.save()

        with transaction.atomic():
            quota = TokenQuota.objects.select_for_update().get(pk=self.quota.pk)
            quota.ensure_periods_reset()

        self.quota.refresh_from_db()
        # Only daily should be reset
        self.assertEqual(self.quota.daily_usage, 0)
        self.assertEqual(self.quota.weekly_usage, 5000)
        self.assertEqual(self.quota.monthly_usage, 20000)
