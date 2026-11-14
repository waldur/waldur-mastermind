import decimal
import json
from unittest import mock

import openportal
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_openportal import models
from waldur_openportal.tests.test_award_membership import MembershipSyncFixture

DESTINATION = "awarding.receiving.cluster"
IDENTIFIER = "award.awarding"
GRACE_NOTE = "not applied: grace period"


class AuditNoteRepeatTest(TestCase):
    """
    A system note written on every resend - e.g. "Allocation change ... was
    not applied: project is in its grace period" - is one event, not one per
    resend. Only callers that ask for it (skip_repeat=True) are folded;
    everything else, user actions above all, is always recorded.
    """

    def setUp(self):
        self.managed_project = models.ManagedProject.objects.create(
            destination=DESTINATION, identifier=IDENTIFIER, details={}
        )
        self.user = structure_factories.UserFactory()

    def record(self, note, event_type=None, **kwargs):
        return models.ManagedProjectAuditEntry.record(
            self.managed_project,
            event_type or models.ManagedProjectAuditEventType.DETAILS_UPDATED,
            note=note,
            **kwargs,
        )

    def notes(self):
        return list(
            models.ManagedProjectAuditEntry.objects.order_by(
                "timestamp", "id"
            ).values_list("note", flat=True)
        )

    def test_repeated_system_note_is_recorded_once(self):
        self.record(GRACE_NOTE, skip_repeat=True)
        self.assertIsNone(self.record(GRACE_NOTE, skip_repeat=True))

        self.assertEqual(self.notes(), [GRACE_NOTE])

    def test_note_repeated_after_another_entry_is_recorded(self):
        self.record(GRACE_NOTE, skip_repeat=True)
        self.record("something else")
        self.record(GRACE_NOTE, skip_repeat=True)

        self.assertEqual(len(self.notes()), 3)

    def test_same_note_as_a_different_event_is_recorded(self):
        self.record("x", skip_repeat=True)
        self.record(
            "x",
            event_type=models.ManagedProjectAuditEventType.NOTE_ADDED,
            skip_repeat=True,
        )

        self.assertEqual(len(self.notes()), 2)

    def test_repeat_is_recorded_without_the_opt_in(self):
        """System entries such as DELETED notes are not folded by default."""
        for _ in range(2):
            self.record(
                "deleted remotely",
                event_type=models.ManagedProjectAuditEventType.DELETED,
            )

        self.assertEqual(len(self.notes()), 2)

    def test_a_user_adding_the_same_note_twice_is_recorded_twice(self):
        for _ in range(2):
            self.record(
                "please check",
                event_type=models.ManagedProjectAuditEventType.NOTE_ADDED,
                performed_by=self.user,
            )

        self.assertEqual(len(self.notes()), 2)

    def test_reattaching_the_same_project_is_recorded_twice(self):
        for _ in range(2):
            self.record(
                "Attached project abc",
                event_type=models.ManagedProjectAuditEventType.PROJECT_ATTACHED,
                performed_by=self.user,
            )

        self.assertEqual(len(self.notes()), 2)

    def test_a_user_action_is_never_folded_even_with_the_opt_in(self):
        for _ in range(2):
            self.record(
                "please check",
                event_type=models.ManagedProjectAuditEventType.NOTE_ADDED,
                performed_by=self.user,
                skip_repeat=True,
            )

        self.assertEqual(len(self.notes()), 2)


class GracePeriodNoteTest(MembershipSyncFixture, TestCase):
    """The board's grace-period note is the one caller that opts in."""

    def test_grace_period_note_asks_to_skip_repeats(self):
        self.project.is_in_grace_period = True
        self.project_template.convert_to_credits.return_value = 2000
        self.project_template.action_is_rejected.return_value = False
        self.utils.get_project_credits.return_value = decimal.Decimal(0)
        details = openportal.AwardDetails(json.dumps({"allocation": "2000 credits"}))

        with mock.patch(
            "waldur_openportal.board.models.ManagedProjectAuditEntry.record"
        ) as record:
            self.update(details)

        grace_calls = [
            call
            for call in record.call_args_list
            if "grace period" in call.kwargs.get("note", "")
        ]
        self.assertEqual(len(grace_calls), 1)
        self.assertIs(grace_calls[0].kwargs.get("skip_repeat"), True)
        self.utils.set_project_credits.assert_not_called()
