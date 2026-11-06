"""The draft-stage mails to an applicant, in the deployment's vocabulary.

A marketplace-only deployment never shows its applicants a call: the
cancellation, deadline-reminder and carried-over mails name the access
request alone there. Every other mode keeps the call's words.
"""

import re
from datetime import timedelta

from django.core import mail
from django.test import TestCase
from django.utils import timezone

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Notification
from waldur_mastermind.proposal import tasks
from waldur_mastermind.proposal.tests import factories, fixtures

REQUEST_NAME = "Weather modelling"
PROSE_ONLY = re.compile(r"https?://\S+|<style\b.*?</style>|<[^>]+>", re.S | re.I)
CALL_WORDS = re.compile(r"\b(calls?|rounds?|proposals?)\b", re.I)


class DraftMailVocabularyTest(TestCase):
    def setUp(self):
        for key in (
            "proposal_cancelled",
            "proposal_submission_deadline_approaching",
            "proposal_draft_carried_over",
        ):
            Notification.objects.update_or_create(
                key=f"proposal.{key}", defaults={"enabled": True}
            )
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.name = REQUEST_NAME
        self.proposal.save()
        self.call = self.proposal.round.call

    def _send(self, mode, send):
        mail.outbox = []
        with override_config(SERVICE_ACCESS_MODE=mode):
            send()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        return [message.subject, message.body, message.alternatives[0][0]]

    def _cancelled(self):
        tasks.notify_proposal_creator_about_cancelled_proposal(
            self.proposal.uuid, "2026-10-09 12:00:00"
        )

    def _reminder(self):
        self.proposal.round.cutoff_time = timezone.now() + timedelta(days=2)
        self.proposal.round.save()
        tasks.notify_proposal_creator_on_submission_deadline_approaching()

    def _carried_over(self):
        previous = factories.RoundFactory(call=self.call)
        tasks.notify_proposal_creator_about_carried_over_draft(
            self.proposal.uuid, previous.uuid
        )

    def assert_names_no_call(self, texts):
        for text in texts:
            prose = PROSE_ONLY.sub(" ", text).replace(REQUEST_NAME, "")
            self.assertIsNone(CALL_WORDS.search(prose), prose)
            self.assertNotIn(self.call.name, text)
        self.assertIn("access request", texts[1])

    def test_marketplace_mode_names_the_access_request(self):
        for send in (self._cancelled, self._reminder, self._carried_over):
            with self.subTest(mail=send.__name__):
                self.assert_names_no_call(self._send("marketplace", send))

    def test_other_modes_name_the_call(self):
        for send in (self._cancelled, self._reminder, self._carried_over):
            with self.subTest(mail=send.__name__):
                texts = self._send("both", send)
                self.assertIn(self.call.name, texts[0] + texts[1])
