"""A call manager emails the applicants of selected proposals."""

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from rest_framework import status, test

from waldur_core.core.models import Notification
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.tests import factories, fixtures


@override_settings(task_always_eager=True)
class NotifyProposalsTest(test.APITestCase):
    def setUp(self):
        structure_factories.NotificationFactory(key="proposal.proposal_manager_message")
        fixture = fixtures.ProposalFixture()
        self.call = fixture.call
        self.call_manager = fixture.call_manager
        self.reviewer = fixture.reviewer_1
        self.proposal1 = factories.ProposalFactory(round=fixture.round)
        self.proposal2 = factories.ProposalFactory(round=fixture.round)
        # Their authors manage them, as creating a proposal makes them.
        for proposal in (self.proposal1, self.proposal2):
            proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        self.member = structure_factories.UserFactory()
        self.proposal1.add_user(self.member, ProposalRole.MEMBER)
        self.url = factories.CallFactory.get_protected_url(
            self.call, action="notify-proposals"
        )

    def post(self, user, **data):
        payload = {
            "proposal_uuids": [self.proposal1.uuid.hex, self.proposal2.uuid.hex],
            "subject": "Missing {budget} annex",
            "body": "Please upload the annex.",
            **data,
        }
        self.client.force_authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url, payload, format="json")

    def test_applicant_of_each_proposal_gets_one_email(self):
        response = self.post(self.call_manager)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["proposals_notified"], 2)
        self.assertEqual(
            sorted(email.to[0] for email in mail.outbox),
            sorted([self.proposal1.created_by.email, self.proposal2.created_by.email]),
        )
        self.assertEqual(mail.outbox[0].subject, "Missing {budget} annex")
        self.assertIn("Please upload the annex.", mail.outbox[0].body)

    def test_team_recipients_include_proposal_members(self):
        self.post(self.call_manager, recipients="team")

        recipients = [email.to[0] for email in mail.outbox]
        self.assertEqual(len(recipients), 3)
        self.assertIn(self.member.email, recipients)

    def test_each_proposal_gets_an_event(self):
        self.post(self.call_manager)

        events = Event.objects.filter(event_type="proposal_notification_sent")
        self.assertEqual(
            {event.context["proposal_uuid"] for event in events},
            {self.proposal1.uuid.hex, self.proposal2.uuid.hex},
        )
        self.assertIn("Missing {budget} annex", events.first().message)

    def test_proposal_of_another_call_is_skipped(self):
        other = factories.ProposalFactory()

        response = self.post(
            self.call_manager, proposal_uuids=[self.proposal1.uuid.hex, other.uuid.hex]
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["proposals_notified"], 1)
        self.assertEqual(len(response.data["skipped_proposals"]), 1)
        self.assertEqual(
            [email.to[0] for email in mail.outbox], [self.proposal1.created_by.email]
        )

    def test_multi_line_subject_is_rejected(self):
        response = self.post(self.call_manager, subject="Line one\nBcc: x@example.com")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(mail.outbox, [])

    def test_applicant_and_reviewer_cannot_notify(self):
        for user in (self.proposal1.created_by, self.reviewer):
            response = self.post(user)
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )
        self.assertEqual(mail.outbox, [])
        self.assertFalse(
            Event.objects.filter(event_type="proposal_notification_sent").exists()
        )

    def test_an_author_who_left_the_team_is_not_told(self):
        manager = structure_factories.UserFactory()
        self.proposal1.add_user(manager, ProposalRole.MANAGER)
        self.proposal1.remove_user(self.proposal1.created_by, ProposalRole.MANAGER)

        self.post(self.call_manager, proposal_uuids=[self.proposal1.uuid.hex])

        self.assertEqual([email.to[0] for email in mail.outbox], [manager.email])

    def test_the_applicant_message_copies_the_roles_the_call_copies(self):
        admin = structure_factories.UserFactory()
        self.proposal1.add_user(admin, ProposalRole.ADMIN)
        self.call.applicant_cc_roles = [RoleEnum.PROPOSAL_ADMIN]
        self.call.save()

        self.post(self.call_manager, proposal_uuids=[self.proposal1.uuid.hex])

        (message,) = mail.outbox
        self.assertEqual(message.to, [self.proposal1.created_by.email])
        self.assertEqual(message.cc, [admin.email])

    def test_the_applicant_reads_it_in_the_proposal_activity(self):
        self.post(self.call_manager, proposal_uuids=[self.proposal1.uuid.hex])

        self.client.force_authenticate(self.proposal1.created_by)
        response = self.client.get(
            "http://testserver" + reverse("event-list"),
            {"scope": factories.ProposalFactory.get_url(self.proposal1)},
        )
        self.assertIn(
            "proposal_notification_sent",
            [row["event_type"] for row in response.data],
        )

    def test_refused_while_the_mail_is_switched_off(self):
        Notification.objects.filter(key="proposal.proposal_manager_message").update(
            enabled=False
        )

        response = self.post(self.call_manager)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(mail.outbox, [])
        self.assertFalse(
            Event.objects.filter(event_type="proposal_notification_sent").exists()
        )

    def test_a_proposal_nobody_on_it_receives_mail_for_is_skipped(self):
        creator = self.proposal2.created_by
        creator.notifications_enabled = False
        creator.save()

        response = self.post(self.call_manager)

        self.assertEqual(response.data["proposals_notified"], 1)
        (skipped,) = response.data["skipped_proposals"]
        self.assertEqual(skipped["proposal_uuid"], self.proposal2.uuid.hex)
        self.assertEqual(
            [email.to[0] for email in mail.outbox], [self.proposal1.created_by.email]
        )
        # Its activity does not say it was written to.
        self.assertEqual(
            {
                event.context["proposal_uuid"]
                for event in Event.objects.filter(
                    event_type="proposal_notification_sent"
                )
            },
            {self.proposal1.uuid.hex},
        )
