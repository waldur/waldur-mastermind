import datetime as dt

from django.test import TestCase
from django.utils import timezone

from waldur_mastermind.chat.anonymous import aggregates, models


def _mk(created, severity="none"):
    return models.AnonymousChatInteraction.objects.create(
        session_id="s1",
        user_slug="u1",
        user_input="x",
        severity=severity,
        created=created,
    )


class AggregatesTest(TestCase):
    def test_daily_volume_pads_zero_days(self):
        today = timezone.now()
        _mk(today)
        _mk(today - dt.timedelta(days=2))
        qs = models.AnonymousChatInteraction.objects.all()
        out = aggregates.daily_volume(qs, days=3)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[-1]["count"], 1)
        self.assertEqual(out[-2]["count"], 0)
        self.assertEqual(out[-3]["count"], 1)

    def test_severity_by_day_buckets_per_severity(self):
        today = timezone.now()
        _mk(today, severity="none")
        _mk(today, severity="high")
        qs = models.AnonymousChatInteraction.objects.all()
        out = aggregates.severity_by_day(qs, days=1)
        self.assertEqual(out["labels"][-1], today.date().isoformat())
        self.assertEqual(out["series"]["NONE"][-1], 1)
        self.assertEqual(out["series"]["HIGH"][-1], 1)
        self.assertEqual(out["series"]["LOW"][-1], 0)

    def test_user_aggregates_groups_by_slug(self):
        today = timezone.now()
        a = _mk(today, severity="none")
        _mk(today, severity="high")
        a.feedback = models.AnonymousChatFeedback.objects.create(
            interaction=a, score=1, comment=""
        )
        qs = models.AnonymousChatInteraction.objects.all()
        out = aggregates.user_aggregates(qs)
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["user_slug"], "u1")
        self.assertEqual(row["total_interactions"], 2)
        self.assertEqual(row["session_count"], 1)
        self.assertEqual(row["positive_feedback"], 1)
        self.assertEqual(row["negative_feedback"], 0)
        self.assertEqual(row["injection_strikes"], 0)

    def test_user_aggregates_enriches_last_seen_and_strikes(self):
        earlier = timezone.now() - dt.timedelta(hours=1)
        later = timezone.now()
        _mk(earlier)
        models.AnonymousChatInteraction.objects.create(
            session_id="s2",
            user_slug="u1",
            user_input="x",
            action_taken="block",
            ip_address="10.0.0.1",
            created=later,
        )
        models.AnonymousChatInteraction.objects.create(
            session_id="s3",
            user_slug="u2",
            user_input="x",
            created=later,
        )
        qs = models.AnonymousChatInteraction.objects.all()
        out = aggregates.user_aggregates(qs)
        self.assertEqual([r["user_slug"] for r in out], ["u1", "u2"])
        u1 = out[0]
        self.assertEqual(u1["injection_strikes"], 1)
        # Raw IPs must not be exposed in the per-user aggregate (the user_slug
        # is the stable pseudonym staff should rely on).
        self.assertNotIn("last_ip", u1)
        self.assertEqual(u1["no_feedback"], 2)
