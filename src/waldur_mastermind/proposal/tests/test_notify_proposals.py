"""A call manager emails the applicants or the reviewers of selected proposals."""

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from rest_framework import status, test

from waldur_core.core.models import Notification
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.models import Review
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

    def test_body_longer_than_the_limit_is_rejected(self):
        response = self.post(self.call_manager, body="x" * 10001)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("body", response.data)
        self.assertEqual(mail.outbox, [])

    def test_body_at_the_limit_is_sent(self):
        response = self.post(self.call_manager, body="x" * 10000)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(mail.outbox)

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

    def test_the_applicant_message_links_to_the_proposal_page(self):
        self.post(self.call_manager, proposal_uuids=[self.proposal1.uuid.hex])

        (message,) = mail.outbox
        self.assertIn(f"proposals/{self.proposal1.uuid.hex}/", message.body)
        self.assertNotIn("proposal-review/", message.body)


@override_settings(task_always_eager=True)
class NotifyProposalReviewersTest(test.APITestCase):
    """A call manager emails the reviewers of selected proposals."""

    def setUp(self):
        structure_factories.NotificationFactory(key="proposal.proposal_manager_message")
        fixture = fixtures.ProposalFixture()
        self.call = fixture.call
        self.call_manager = fixture.call_manager
        self.reviewer_1 = fixture.reviewer_1
        self.reviewer_2 = fixture.reviewer_2
        self.proposal1 = factories.ProposalFactory(round=fixture.round)
        self.proposal2 = factories.ProposalFactory(round=fixture.round)
        for proposal in (self.proposal1, self.proposal2):
            proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        self.member = structure_factories.UserFactory()
        self.proposal1.add_user(self.member, ProposalRole.MEMBER)
        self.review_1_1 = factories.ReviewFactory(
            proposal=self.proposal1, reviewer=self.reviewer_1
        )
        self.review_1_2 = factories.ReviewFactory(
            proposal=self.proposal1,
            reviewer=self.reviewer_2,
            state=Review.States.SUBMITTED,
        )
        self.review_2_1 = factories.ReviewFactory(
            proposal=self.proposal2, reviewer=self.reviewer_1
        )
        self.url = factories.CallFactory.get_protected_url(
            self.call, action="notify-proposals"
        )

    def post(self, user, **data):
        payload = {
            "proposal_uuids": [self.proposal1.uuid.hex, self.proposal2.uuid.hex],
            "recipients": "reviewers",
            "subject": "Review deadline moved",
            "body": "Reviews are now due on Friday.",
            **data,
        }
        mail.outbox.clear()
        self.client.force_authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url, payload, format="json")

    def review_link(self, review):
        return f"proposal-review/{review.uuid.hex}/"

    def events(self, event_type="proposal_reviewers_notified"):
        return Event.objects.filter(event_type=event_type)

    def feed_types(self, user, proposal):
        self.client.force_authenticate(user)
        response = self.client.get(
            "http://testserver" + reverse("event-list"),
            {"scope": factories.ProposalFactory.get_url(proposal)},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row["event_type"] for row in response.data]

    def test_each_reviewer_gets_a_message_of_their_own(self):
        response = self.post(self.call_manager)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["proposals_notified"], 2)
        self.assertEqual(response.data["skipped_proposals"], [])
        self.assertEqual(len(mail.outbox), 3)
        for message in mail.outbox:
            self.assertEqual(len(message.to), 1)
            self.assertEqual(message.cc, [])
            self.assertEqual(message.bcc, [])
            self.assertEqual(message.subject, "Review deadline moved")
            self.assertIn("Reviews are now due on Friday.", message.body)
        self.assertEqual(
            sorted(message.to[0] for message in mail.outbox),
            sorted([self.reviewer_1.email] * 2 + [self.reviewer_2.email]),
        )

    def test_a_reviewer_of_two_proposals_gets_one_message_per_review(self):
        self.post(self.call_manager)

        bodies = [m.body for m in mail.outbox if m.to == [self.reviewer_1.email]]
        self.assertEqual(len(bodies), 2)
        for review in (self.review_1_1, self.review_2_1):
            (body,) = [b for b in bodies if self.review_link(review) in b]
            self.assertIn(review.proposal.name, body)
        (body,) = [m.body for m in mail.outbox if m.to == [self.reviewer_2.email]]
        self.assertIn(self.review_link(self.review_1_2), body)
        # Their own review, not the applicant's proposal page.
        for message in mail.outbox:
            self.assertNotIn("/proposals/", message.body)
            self.assertNotIn("/proposals/", message.alternatives[0][0])

    def test_no_message_names_another_reviewer_or_the_applicant_team(self):
        self.post(self.call_manager)

        applicants = [
            self.proposal1.created_by,
            self.proposal2.created_by,
            self.member,
        ]
        for message in mail.outbox:
            others = [
                user
                for user in (self.reviewer_1, self.reviewer_2)
                if user.email != message.to[0]
            ] + applicants
            text = message.body + message.alternatives[0][0]
            for user in others:
                self.assertNotIn(user.email, message.to + message.cc + message.bcc)
                self.assertNotIn(user.email, text)
                self.assertNotIn(user.full_name, text)

    def test_only_live_reviews_of_mail_eligible_reviewers_are_written_to(self):
        rejected = factories.ReviewFactory(
            proposal=self.proposal1, state=Review.States.REJECTED
        )
        inactive = factories.ReviewFactory(
            proposal=self.proposal1,
            reviewer=structure_factories.UserFactory(is_active=False),
        )
        no_email = factories.ReviewFactory(
            proposal=self.proposal1, reviewer=structure_factories.UserFactory(email="")
        )
        opted_out = factories.ReviewFactory(proposal=self.proposal1)
        opted_out.reviewer.notifications_enabled = False
        opted_out.reviewer.save()

        self.post(self.call_manager, proposal_uuids=[self.proposal1.uuid.hex])

        self.assertEqual(
            sorted(message.to[0] for message in mail.outbox),
            sorted([self.reviewer_1.email, self.reviewer_2.email]),
        )
        for review in (rejected, inactive, no_email, opted_out):
            for message in mail.outbox:
                self.assertNotIn(self.review_link(review), message.body)

    def test_a_reviewer_on_the_applicant_team_is_not_written_to(self):
        self.proposal1.add_user(self.reviewer_2, ProposalRole.MEMBER)

        self.post(self.call_manager, proposal_uuids=[self.proposal1.uuid.hex])

        self.assertEqual(
            [message.to[0] for message in mail.outbox], [self.reviewer_1.email]
        )

    def test_a_call_manager_on_the_applicant_team_cannot_write_to_its_reviewers(
        self,
    ):
        self.proposal1.add_user(self.call_manager, ProposalRole.MEMBER)

        response = self.post(self.call_manager)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["proposals_notified"], 1)
        (skipped,) = response.data["skipped_proposals"]
        self.assertEqual(skipped["proposal_uuid"], self.proposal1.uuid.hex)
        self.assertIn("applicant team", skipped["reason"])
        self.assertEqual(
            [message.to[0] for message in mail.outbox], [self.reviewer_1.email]
        )
        self.assertNotIn(self.review_link(self.review_1_1), mail.outbox[0].body)
        self.assertEqual(
            {event.context["proposal_uuid"] for event in self.events()},
            {self.proposal2.uuid.hex},
        )

    def test_a_proposal_without_a_live_review_is_skipped(self):
        self.review_2_1.state = Review.States.REJECTED
        self.review_2_1.save()

        response = self.post(self.call_manager)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["proposals_notified"], 1)
        (skipped,) = response.data["skipped_proposals"]
        self.assertEqual(skipped["proposal_uuid"], self.proposal2.uuid.hex)
        self.assertEqual(skipped["proposal_name"], self.proposal2.name)
        self.assertIn("no review", skipped["reason"])
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            {event.context["proposal_uuid"] for event in self.events()},
            {self.proposal1.uuid.hex},
        )

    def test_a_proposal_whose_reviewers_receive_no_mail_is_skipped(self):
        self.reviewer_1.notifications_enabled = False
        self.reviewer_1.save()

        response = self.post(self.call_manager)

        (skipped,) = response.data["skipped_proposals"]
        self.assertEqual(skipped["proposal_uuid"], self.proposal2.uuid.hex)
        self.assertIn("receives mail", skipped["reason"])
        self.assertEqual(
            [message.to[0] for message in mail.outbox], [self.reviewer_2.email]
        )

    def test_the_event_is_for_the_call_team_not_the_applicant_team(self):
        self.post(self.call_manager, proposal_uuids=[self.proposal1.uuid.hex])

        (event,) = self.events()
        self.assertIn("reviewers", event.message)
        self.assertIn("Review deadline moved", event.message)
        for reviewer in (self.reviewer_1, self.reviewer_2):
            self.assertNotIn(reviewer.full_name, event.message)
            self.assertNotIn(reviewer.email, str(event.context))
        self.assertFalse(self.events("proposal_notification_sent").exists())

        staff = structure_factories.UserFactory(is_staff=True)
        support = structure_factories.UserFactory(is_support=True)
        for user in (self.call_manager, staff, support):
            with self.subTest(user=user.username):
                self.assertIn(
                    "proposal_reviewers_notified",
                    self.feed_types(user, self.proposal1),
                )
        for user in (self.proposal1.created_by, self.member):
            with self.subTest(user=user.username):
                self.assertNotIn(
                    "proposal_reviewers_notified",
                    self.feed_types(user, self.proposal1),
                )

    def test_applicant_and_reviewer_cannot_notify_reviewers(self):
        for user in (self.proposal1.created_by, self.reviewer_1):
            response = self.post(user)
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )
        self.assertEqual(mail.outbox, [])
        self.assertFalse(self.events().exists())

    def test_refused_while_the_mail_is_switched_off(self):
        Notification.objects.filter(key="proposal.proposal_manager_message").update(
            enabled=False
        )

        response = self.post(self.call_manager)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(mail.outbox, [])
        self.assertFalse(self.events().exists())
