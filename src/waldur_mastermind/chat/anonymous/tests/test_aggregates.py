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

    def test_session_aggregates_groups_by_session(self):
        now = timezone.now()
        # Conversation s1: two messages; one flagged high, first has 👍 feedback.
        a = models.AnonymousChatInteraction.objects.create(
            session_id="s1",
            user_slug="u1",
            user_input="hi",
            result_count=2,
            created=now,
        )
        models.AnonymousChatInteraction.objects.create(
            session_id="s1",
            user_slug="u1",
            user_input="bad",
            is_flagged=True,
            severity="high",
            result_count=1,
            created=now + dt.timedelta(minutes=1),
        )
        models.AnonymousChatFeedback.objects.create(interaction=a, score=1, comment="")
        # Conversation s2: one clean message.
        models.AnonymousChatInteraction.objects.create(
            session_id="s2", user_slug="u2", user_input="hey", created=now
        )

        out = aggregates.session_aggregates(
            models.AnonymousChatInteraction.objects.all()
        )
        by_session = {row["session_id"]: row for row in out}
        self.assertEqual(len(out), 2)

        s1 = by_session["s1"]
        self.assertEqual(s1["user_slug"], "u1")
        self.assertEqual(s1["message_count"], 2)
        self.assertTrue(s1["is_flagged"])
        self.assertEqual(s1["max_severity"], "high")
        self.assertTrue(s1["has_feedback"])
        self.assertEqual(s1["offerings_shown"], 3)

        s2 = by_session["s2"]
        self.assertEqual(s2["message_count"], 1)
        self.assertFalse(s2["is_flagged"])
        self.assertEqual(s2["max_severity"], "none")
        self.assertFalse(s2["has_feedback"])

    def test_session_aggregates_collapses_to_highest_severity_label(self):
        """Each conversation reports the highest severity seen, decoded back to
        its label. Covers every rung (critical/high/medium/low), not just
        high-vs-none, so the SQL rank encode and the label decode stay aligned."""
        now = timezone.now()
        # One conversation per level: a clean message plus a rated one, so the
        # Max(rank) collapse actually has to pick the higher of the two.
        for session_id, severity in (
            ("sc", "critical"),
            ("sh", "high"),
            ("sm", "medium"),
            ("sl", "low"),
        ):
            models.AnonymousChatInteraction.objects.create(
                session_id=session_id,
                user_slug="u1",
                user_input="clean",
                created=now,
            )
            models.AnonymousChatInteraction.objects.create(
                session_id=session_id,
                user_slug="u1",
                user_input="rated",
                severity=severity,
                created=now + dt.timedelta(minutes=1),
            )

        by_session = {
            row["session_id"]: row
            for row in aggregates.session_aggregates(
                models.AnonymousChatInteraction.objects.all()
            )
        }
        self.assertEqual(by_session["sc"]["max_severity"], "critical")
        self.assertEqual(by_session["sh"]["max_severity"], "high")
        self.assertEqual(by_session["sm"]["max_severity"], "medium")
        self.assertEqual(by_session["sl"]["max_severity"], "low")

    def test_session_aggregates_counts_clicks_without_inflating_other_columns(self):
        """Clicks is a non-unique FK: counting it via another annotate() on the
        same queryset would multiply the interaction rows, so message_count and
        offerings_shown must stay correct when several clicks exist."""
        now = timezone.now()
        a = models.AnonymousChatInteraction.objects.create(
            session_id="s1",
            user_slug="u1",
            user_input="hi",
            result_count=2,
            created=now,
        )
        models.AnonymousChatInteraction.objects.create(
            session_id="s1",
            user_slug="u1",
            user_input="more",
            result_count=1,
            created=now + dt.timedelta(minutes=1),
        )
        # Three clicks on one interaction, including a repeat on the same
        # offering — repeat clicks are intentional and counted separately.
        repeated = "11111111-1111-1111-1111-111111111111"
        for offering_uuid in (
            repeated,
            repeated,
            "22222222-2222-2222-2222-222222222222",
        ):
            models.AnonymousChatClick.objects.create(
                interaction=a, offering_uuid=offering_uuid
            )
        # A conversation with no clicks must report 0, not be missing the key.
        models.AnonymousChatInteraction.objects.create(
            session_id="s2", user_slug="u2", user_input="hey", created=now
        )

        by_session = {
            row["session_id"]: row
            for row in aggregates.session_aggregates(
                models.AnonymousChatInteraction.objects.all()
            )
        }

        s1 = by_session["s1"]
        self.assertEqual(s1["offerings_clicked"], 3)
        # These two would be inflated to 6 and 9 by a naive Count("clicks") join.
        self.assertEqual(s1["message_count"], 2)
        self.assertEqual(s1["offerings_shown"], 3)
        self.assertEqual(by_session["s2"]["offerings_clicked"], 0)

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
