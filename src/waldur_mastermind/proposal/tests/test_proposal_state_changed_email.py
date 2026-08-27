"""The applicant-facing state-change mail.

Two things are covered: the accepted block, whose duration and allocation date
were both broken, and the vocabulary, which must follow SERVICE_ACCESS_MODE so
the mail agrees with the UI that produced it.
"""

import datetime
import re

from constance.test import override_config
from ddt import data, ddt
from django.core import mail
from django.test import TestCase
from django.utils import timezone

from waldur_core.core.models import Notification
from waldur_mastermind.proposal import tasks
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import fixtures

# The fixture's proposal is called "New Proposal", which would trip every
# assertion that the word never appears.
REQUEST_NAME = "Weather modelling"

# The applicant's own link is /proposals/<uuid>/ in every mode — a route, not
# prose, and not this MR's to rename. CSS and markup are not prose either.
PROSE_ONLY = re.compile(r"https?://\S+|<style\b.*?</style>|<[^>]+>", re.S | re.I)
CALL_WORDS = re.compile(r"\b(calls?|rounds?|proposals?|reviewers?)\b", re.I)


class BaseStateChangedEmailTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.name = REQUEST_NAME
        self.proposal.save()
        # One switch for the event, whichever wording it goes out in.
        # broadcast_mail returns silently when the row is missing or disabled,
        # so a test would otherwise assert on an empty outbox.
        Notification.objects.update_or_create(
            key="proposal.proposal_state_changed", defaults={"enabled": True}
        )

    # Which deployment this class is testing. Applied around each send rather
    # than as a class decorator: override_config writes through to the Constance
    # database backend, and a class decorator leaves the value behind for
    # whatever runs next against the same database.
    mode = "both"

    def notify(self, new_state=ProposalStates.ACCEPTED, mode=None):
        mail.outbox = []
        with override_config(SERVICE_ACCESS_MODE=mode or self.mode):
            tasks.notify_user_about_proposal_state_update(
                self.proposal.uuid, ProposalStates.IN_REVIEW, new_state
            )
        return mail.outbox[0]

    def grant(self, start_date, end_date):
        """Give the proposal an allocated project running between two dates."""
        project = self.proposal.project
        project.start_date = start_date
        project.end_date = end_date
        project.save()
        return project


class DurationTest(BaseStateChangedEmailTest):
    mode = "marketplace"

    """The access-request message: nothing asks its applicant for a duration,
    so it reports what was granted, or omits the line."""

    def test_no_duration_line_when_the_grant_does_not_expire(self):
        # The regression the frontend would otherwise ship: with the applicant
        # no longer asked for a duration, the old context passed None straight
        # into the template, which rendered it as the string "None".
        self.grant(timezone.localdate(), None)
        self.proposal.duration_in_days = None
        self.proposal.save()

        message = self.notify()

        self.assertNotIn("Duration", message.body)
        self.assertNotIn("None", message.body)
        self.assertNotIn("None", message.alternatives[0][0])

    def test_reports_the_granted_project_period(self):
        start = timezone.localdate()
        self.grant(start, start + datetime.timedelta(days=180))

        message = self.notify()

        self.assertIn("Duration: 180 days", message.body)

    def test_measures_from_creation_when_the_project_has_no_start_date(self):
        created = timezone.localdate(self.proposal.project.created)
        self.grant(None, created + datetime.timedelta(days=90))

        message = self.notify()

        self.assertIn("Duration: 90 days", message.body)

    def test_reports_what_was_granted_rather_than_what_was_asked_for(self):
        # duration_in_days records the request, is not collected at all in this
        # mode, and nothing in allocation reads it any more.
        start = timezone.localdate()
        self.grant(start, start + datetime.timedelta(days=180))
        self.proposal.duration_in_days = 10
        self.proposal.save()

        message = self.notify()

        self.assertIn("Duration: 180 days", message.body)
        self.assertNotIn("10 days", message.body)

    def test_allocation_start_date_is_populated(self):
        # Declared on the context model and rendered by both bodies since day
        # one, but never passed — the line shipped blank.
        start = timezone.localdate()
        self.grant(start, start + datetime.timedelta(days=30))

        message = self.notify()

        # The defect was a blank line, so what matters is that something
        # follows the label. The date itself renders through Django's
        # localisation and is not ISO.
        self.assertRegex(message.body, r"Allocation start date: \S+")
        self.assertIn(str(start.year), message.body)

    def test_the_accepted_block_is_absent_for_other_states(self):
        message = self.notify(ProposalStates.REJECTED)

        self.assertNotIn("Duration", message.body)
        self.assertNotIn("Allocation start date", message.body)


@ddt
class CallVocabularyTest(BaseStateChangedEmailTest):
    """Call-managed deployments must read exactly as they do today."""

    @data("both", "calls")
    def test_the_call_is_named(self, mode):
        message = self.notify(ProposalStates.SUBMITTED, mode=mode)

        self.assertEqual(
            message.subject,
            f"Proposal state update: {REQUEST_NAME} - {ProposalStates.SUBMITTED}",
        )
        self.assertIn(
            f'The state of your proposal "{REQUEST_NAME}" in call '
            f'"{self.proposal.round.call.name}" has been updated.',
            message.body,
        )
        self.assertIn("View Proposal:", message.body)

    @data("submitted", "in_review", "accepted", "rejected")
    def test_each_state_keeps_its_sentence(self, state):
        sentences = {
            "submitted": "Your proposal has been successfully submitted",
            "in_review": "Your proposal is now under review.",
            "accepted": "Congratulations! Your proposal has been accepted.",
            "rejected": "We regret to inform you that your proposal has not been accepted",
        }
        message = self.notify(state, mode="both")

        self.assertIn(sentences[state], message.body)


class NotificationSelectionTest(BaseStateChangedEmailTest):
    def test_the_two_deployments_send_different_notifications(self):
        self.assertIn("Proposal state update", self.notify(mode="both").subject)
        self.assertIn("Access request update", self.notify(mode="marketplace").subject)

    def test_one_switch_governs_both_wordings(self):
        # A deployment is in one mode and only ever sends one of the two, so the
        # operator gets a single "tell the applicant" switch rather than one per
        # wording, and neither can be left on by forgetting the other.
        Notification.objects.filter(key="proposal.proposal_state_changed").update(
            enabled=False
        )

        for mode in ("both", "marketplace"):
            mail.outbox = []
            with override_config(SERVICE_ACCESS_MODE=mode):
                tasks.notify_user_about_proposal_state_update(
                    self.proposal.uuid,
                    ProposalStates.IN_REVIEW,
                    ProposalStates.ACCEPTED,
                )
            self.assertEqual(mail.outbox, [], f"{mode} still sent mail")

    def test_both_template_sets_are_declared_by_the_notification(self):
        # find_template_from_registry honours a variant only where the
        # notification declares it, so this is what keeps the marketplace
        # wording reachable at all.
        from waldur_core.core.utils import find_template_from_registry

        self.assertEqual(
            find_template_from_registry(
                "proposal",
                "proposal_state_changed",
                "message.txt",
                "access_request_state_changed",
            ),
            "proposal/access_request_state_changed_message.txt",
        )


@ddt
class AccessRequestVocabularyTest(BaseStateChangedEmailTest):
    mode = "marketplace"

    @data("submitted", "in_review", "accepted", "rejected")
    def test_no_call_vocabulary_in_either_body(self, state):
        # Asserted against both bodies: they are separate files and this is
        # what stops them drifting apart.
        message = self.notify(state)

        for body in (message.subject, message.body, message.alternatives[0][0]):
            prose = PROSE_ONLY.sub(" ", body)
            leaked = CALL_WORDS.findall(prose)
            self.assertEqual(leaked, [], f"{leaked} leaked into:\n{prose}")
            self.assertNotIn(self.proposal.round.call.name, prose)
            self.assertNotIn(self.proposal.round.name, prose)

    def test_access_request_wording(self):
        message = self.notify(ProposalStates.SUBMITTED)

        self.assertEqual(
            message.subject,
            f"Access request update: {REQUEST_NAME} - {ProposalStates.SUBMITTED}",
        )
        self.assertIn(
            f'The state of your access request "{REQUEST_NAME}" has been updated.',
            message.body,
        )
        self.assertIn("View access request:", message.body)

    def test_the_review_period_is_not_quoted(self):
        # It is round.review_duration_in_days — a round-level concept, and the
        # marketplace sentence must not offer to wait "None days".
        self.proposal.round.review_duration_in_days = None
        self.proposal.round.save()

        message = self.notify(ProposalStates.IN_REVIEW)

        self.assertNotIn("None", message.body)
        self.assertNotIn("days", message.body)
