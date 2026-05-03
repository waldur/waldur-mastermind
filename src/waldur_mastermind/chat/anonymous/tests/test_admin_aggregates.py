from django.test import TestCase
from rest_framework.test import APIClient

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.anonymous import models


class KpiEndpointTest(TestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff)
        models.AnonymousChatInteraction.objects.create(
            session_id="s1",
            user_slug="u1",
            user_input="x",
            severity="none",
        )

    def test_kpi_includes_aggregate_series(self):
        res = self.client.get("/api/anonymous-chat-interactions/kpi/")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("daily_volume", body)
        self.assertIn("severity_by_day", body)
        # Daily volume defaults to 30-day window unless window_days supplied
        self.assertEqual(len(body["daily_volume"]), 30)
        # Severity series always exposes all 5 levels
        self.assertEqual(
            sorted(body["severity_by_day"]["series"].keys()),
            ["CRITICAL", "HIGH", "LOW", "MEDIUM", "NONE"],
        )

    def test_kpi_window_days_overrides(self):
        res = self.client.get("/api/anonymous-chat-interactions/kpi/?window_days=7")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["daily_volume"]), 7)


class ByUserListTest(TestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff)

    def test_by_user_list_groups_per_slug(self):
        models.AnonymousChatInteraction.objects.create(
            session_id="s1",
            user_slug="u1",
            user_input="x",
            severity="none",
        )
        models.AnonymousChatInteraction.objects.create(
            session_id="s2",
            user_slug="u1",
            user_input="y",
            severity="none",
        )
        models.AnonymousChatInteraction.objects.create(
            session_id="s3",
            user_slug="u2",
            user_input="z",
            severity="none",
        )
        res = self.client.get("/api/anonymous-chat-interactions/by-user/")
        self.assertEqual(res.status_code, 200)
        rows = {r["user_slug"]: r for r in res.json()}
        self.assertEqual(rows["u1"]["total_interactions"], 2)
        self.assertEqual(rows["u1"]["session_count"], 2)
        self.assertEqual(rows["u2"]["total_interactions"], 1)


class BudgetSnapshotTest(TestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff)

    def test_budget_snapshot_returns_used_and_limits(self):
        res = self.client.get("/api/anonymous-chat-interactions/budget/")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        for key in ("tokens_today", "tokens_limit", "resets_at"):
            self.assertIn(key, body)
        # Sanity: limit should be a non-negative integer (0 means no cap configured)
        self.assertIsInstance(body["tokens_limit"], int)
