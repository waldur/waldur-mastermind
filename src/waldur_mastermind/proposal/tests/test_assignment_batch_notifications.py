"""Reviewer assignment batches: invitation email, reminders, deadline extension
and the reviewer's access to the proposals they accepted."""

from datetime import date, timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.notifications import NOTIFICATIONS
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, tasks
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    ProposalDisclosureLevels,
    ReviewerPoolInvitationStatuses,
)

from . import factories


class AssignmentBatchTestMixin:
    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)

        self.call = factories.CallFactory()
        self.call_manager = structure_factories.UserFactory(email="manager@example.com")
        self.call.add_user(self.call_manager, CallRole.MANAGER)
        self.call.manager.add_user(self.call_manager, CallRole.MANAGER)

        self.reviewer_user = structure_factories.UserFactory(
            email="reviewer@example.com"
        )
        self.reviewer_profile = factories.ReviewerProfileFactory(
            user=self.reviewer_user
        )
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer_profile,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(
            round=self.round,
            name="Protein folding at scale",
            project_summary="Secret summary of the work",
        )

        for key in (
            "proposal.reviewer_assignment_invitation",
            "proposal.assignment_expiry_reminder",
            "proposal.assignment_batch_expired",
        ):
            structure_factories.NotificationFactory(key=key)

    def _create_batch(self, status=AssignmentBatchStatuses.DRAFT, **kwargs):
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.pool_entry,
            status=status,
            **kwargs,
        )
        self.item = factories.AssignmentItemFactory(
            batch=batch,
            proposal=self.proposal,
            status=AssignmentItemStatuses.PENDING,
        )
        return batch


class NotificationRegistryTest(test.APITestCase):
    def test_assignment_notifications_are_registered(self):
        registered = {entry["path"] for entry in NOTIFICATIONS["proposal"]}
        for key in (
            "reviewer_assignment_invitation",
            "assignment_expiry_reminder",
            "assignment_batch_expired",
        ):
            self.assertIn(key, registered)


@override_settings(task_always_eager=True)
class SendAssignmentBatchInvitationTest(AssignmentBatchTestMixin, test.APITestCase):
    def test_sending_batch_emails_reviewer_once(self):
        batch = self._create_batch()
        self.client.force_authenticate(self.call_manager)

        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="send"),
            {"manager_notes": "Please respond this week"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["reviewer@example.com"])
        self.assertIn(self.call.name, message.subject)
        self.assertIn("Protein folding at scale", message.body)
        self.assertIn("Please respond this week", message.body)
        self.assertIn("reviews/assignments/", message.body)
        batch.refresh_from_db()
        self.assertIn(str(batch.expires_at.year), message.body)

    def test_titles_only_disclosure_hides_summary(self):
        batch = self._create_batch()
        self.client.force_authenticate(self.call_manager)
        self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="send"),
            {},
            format="json",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("Secret summary of the work", mail.outbox[0].body)

    def test_summary_disclosure_includes_summary(self):
        factories.CallCOIConfigurationFactory(
            call=self.call,
            invitation_proposal_disclosure=ProposalDisclosureLevels.TITLES_AND_SUMMARIES,
        )
        batch = self._create_batch()
        self.client.force_authenticate(self.call_manager)
        self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="send"),
            {},
            format="json",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Secret summary of the work", mail.outbox[0].body)

    def test_coi_blocked_proposal_is_listed_by_title_only(self):
        factories.CallCOIConfigurationFactory(
            call=self.call,
            invitation_proposal_disclosure=ProposalDisclosureLevels.FULL_DETAILS,
        )
        batch = self._create_batch()
        blocked = factories.ProposalFactory(
            round=self.round,
            name="Conflicted proposal",
            project_summary="Summary the reviewer must not see",
        )
        factories.AssignmentItemFactory(
            batch=batch,
            proposal=blocked,
            status=AssignmentItemStatuses.COI_BLOCKED,
            has_coi=True,
        )
        self.client.force_authenticate(self.call_manager)
        self.client.post(
            factories.AssignmentBatchFactory.get_url(batch, action="send"),
            {},
            format="json",
        )

        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("Conflicted proposal", body)
        self.assertNotIn("Summary the reviewer must not see", body)
        self.assertIn("Secret summary of the work", body)

    def test_send_all_assignments_emails_each_reviewer(self):
        self._create_batch()
        self.client.force_authenticate(self.call_manager)

        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="send-all-assignments"
            ),
            {},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["batches_sent"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["reviewer@example.com"])


class AssignmentReminderTasksTest(AssignmentBatchTestMixin, test.APITestCase):
    def test_expiry_reminder_is_sent(self):
        batch = self._create_batch(
            status=AssignmentBatchStatuses.SENT,
            sent_at=timezone.now() - timedelta(days=6),
            expires_at=timezone.now() + timedelta(days=1),
        )

        tasks.send_assignment_expiry_reminders()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["reviewer@example.com"])
        self.assertIn(self.call.name, mail.outbox[0].subject)
        self.assertIn("reviews/assignments/", mail.outbox[0].body)
        batch.refresh_from_db()
        self.assertTrue(batch.reminder_sent)

    def test_expiry_reminder_is_not_sent_outside_window(self):
        self._create_batch(
            status=AssignmentBatchStatuses.SENT,
            sent_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )

        tasks.send_assignment_expiry_reminders()

        self.assertEqual(len(mail.outbox), 0)

    def test_managers_are_notified_of_expired_batches(self):
        batch = self._create_batch(
            status=AssignmentBatchStatuses.EXPIRED,
            sent_at=timezone.now() - timedelta(days=8),
            expires_at=timezone.now() - timedelta(days=1),
        )

        tasks.notify_managers_of_expired_batches()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["manager@example.com"])
        self.assertIn(self.call.name, mail.outbox[0].subject)
        batch.refresh_from_db()
        self.assertTrue(batch.manager_notified)

    def test_expired_batch_email_links_to_assignment_batches_tab(self):
        self._create_batch(
            status=AssignmentBatchStatuses.EXPIRED,
            sent_at=timezone.now() - timedelta(days=8),
            expires_at=timezone.now() - timedelta(days=1),
        )

        tasks.notify_managers_of_expired_batches()

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(
            f"call/{self.call.uuid.hex}/manage/?tab=reviewer-pool"
            "&pool_tab=assignment_batches",
            mail.outbox[0].body,
        )

    def test_expiry_reminder_is_not_sent_after_deadline(self):
        batch = self._create_batch(
            status=AssignmentBatchStatuses.SENT,
            sent_at=timezone.now() - timedelta(days=8),
            expires_at=timezone.now() - timedelta(hours=1),
        )

        tasks.send_assignment_expiry_reminders()

        self.assertEqual(len(mail.outbox), 0)
        batch.refresh_from_db()
        self.assertFalse(batch.reminder_sent)

    def test_emails_do_not_end_a_date_with_a_double_full_stop(self):
        # Django's default datetime format ends in "a.m." / "p.m.", so a
        # template must not follow a date with its own full stop.
        expires_at = (timezone.now() + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        self._create_batch(
            status=AssignmentBatchStatuses.SENT,
            sent_at=timezone.now() - timedelta(days=6),
            expires_at=expires_at,
        )
        self._create_batch_for_expiry_notice()

        tasks.send_assignment_expiry_reminders()
        tasks.notify_managers_of_expired_batches()
        tasks.send_assignment_batch_invitation(
            self._create_batch(expires_at=expires_at).uuid
        )

        self.assertEqual(len(mail.outbox), 3)
        for message in mail.outbox:
            self.assertNotIn("..", message.body)
            html = message.alternatives[0][0]
            self.assertNotIn(".</strong>.", html)
            self.assertNotIn("..", html.replace("...", ""))

    def _create_batch_for_expiry_notice(self):
        pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=pool_entry,
            status=AssignmentBatchStatuses.EXPIRED,
            sent_at=timezone.now().replace(hour=10, minute=0) - timedelta(days=8),
            expires_at=timezone.now().replace(hour=10, minute=0) - timedelta(days=1),
        )
        factories.AssignmentItemFactory(
            batch=batch, proposal=factories.ProposalFactory(round=self.round)
        )
        return batch


class ExtendDeadlineMovesReviewDeadlineTest(AssignmentBatchTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.round.review_duration_in_days = 5
        self.round.save()
        self.batch = self._create_batch(
            status=AssignmentBatchStatuses.SENT,
            sent_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=3),
        )
        # A second proposal still awaits an answer, so the batch stays SENT
        # (and its deadline extendable) after the first one is accepted.
        factories.AssignmentItemFactory(
            batch=self.batch,
            proposal=factories.ProposalFactory(round=self.round),
            status=AssignmentItemStatuses.PENDING,
        )
        self.review = self.item.accept()

    def test_extend_deadline_succeeds_and_moves_review_deadline(self):
        new_deadline = timezone.now() + timedelta(days=30)
        self.client.force_authenticate(self.call_manager)

        response = self.client.post(
            factories.AssignmentBatchFactory.get_url(
                self.batch, action="extend-deadline"
            ),
            {"expires_at": new_deadline.isoformat()},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        review = models.Review.objects.get(pk=self.review.pk)
        self.assertEqual(review.review_end_date, new_deadline)

    def test_review_deadline_is_not_shortened_by_earlier_batch_deadline(self):
        review = models.Review.objects.get(pk=self.review.pk)
        self.assertEqual(review.review_end_date, review.created + timedelta(days=5))

    def test_expiry_task_respects_extended_batch_deadline(self):
        self.batch.expires_at = timezone.now() + timedelta(days=30)
        self.batch.save()

        with freeze_time(timezone.now() + timedelta(days=10)):
            tasks.expired_reviews_should_be_cancelled()

        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.IN_REVIEW)

        with freeze_time(timezone.now() + timedelta(days=31)):
            tasks.expired_reviews_should_be_cancelled()

        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.REJECTED)

    def test_expiry_task_skips_reviews_without_deadline(self):
        self.round.review_duration_in_days = None
        self.round.save()

        with freeze_time(timezone.now() + timedelta(days=365)):
            tasks.expired_reviews_should_be_cancelled()

        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.IN_REVIEW)


class AcceptedAssignmentGrantsProposalAccessTest(
    AssignmentBatchTestMixin, test.APITestCase
):
    def setUp(self):
        super().setUp()
        self.batch = self._create_batch(
            status=AssignmentBatchStatuses.SENT,
            sent_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.url = factories.ProposalFactory.get_url(self.proposal)

    def test_reviewer_cannot_read_proposal_before_accepting(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_reviewer_can_read_proposal_after_accepting(self):
        self.client.force_authenticate(self.reviewer_user)
        accept_url = factories.AssignmentItemFactory.get_url(self.item) + "accept/"
        self.assertEqual(self.client.post(accept_url).status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.proposal.uuid.hex)

    def test_reviewer_loses_access_when_review_is_rejected(self):
        review = self.item.accept()
        review.state = models.Review.States.REJECTED
        review.save()
        self.client.force_authenticate(self.reviewer_user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ExtendedBatchMovesQueriedDeadlineTest(AssignmentBatchTestMixin, test.APITestCase):
    """The SQL deadline used by dashboards and filters agrees with the property."""

    def setUp(self):
        super().setUp()
        self.round.review_duration_in_days = 5
        self.round.save()
        self.batch = self._create_batch(
            status=AssignmentBatchStatuses.SENT,
            sent_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=3),
        )
        self.review = self.item.accept()
        self.unassigned_review = factories.ReviewFactory(
            proposal=factories.ProposalFactory(round=self.round)
        )

    def test_due_within_uses_round_duration_when_batch_ends_earlier(self):
        due = models.Review.objects.due_within(7)
        self.assertIn(self.review, due)
        self.assertIn(self.unassigned_review, due)

    def test_extended_batch_moves_deadline_in_due_within(self):
        new_deadline = timezone.now() + timedelta(days=30)
        self.batch.expires_at = new_deadline
        self.batch.save()

        due = models.Review.objects.due_within(7)
        self.assertNotIn(self.review, due)
        self.assertIn(self.unassigned_review, due)
        self.assertIn(self.review, models.Review.objects.due_within(31))
        self.assertTrue(
            models.Review.objects.with_deadline()
            .filter(pk=self.review.pk, review_deadline=new_deadline)
            .exists()
        )

    def test_no_round_duration_means_no_deadline(self):
        self.round.review_duration_in_days = None
        self.round.save()
        self.batch.expires_at = timezone.now() + timedelta(days=30)
        self.batch.save()

        self.assertFalse(
            models.Review.objects.with_deadline().filter(pk=self.review.pk).exists()
        )


class ReviewPathReviewerApplicantVisibilityTest(
    AssignmentBatchTestMixin, test.APITestCase
):
    """A reviewer who reads the proposal through an accepted assignment sees the
    applicant only as far as the call's visibility config allows — the same as a
    CALL.REVIEWER holder, and never the unfiltered record."""

    HIDDEN_FIELDS = (
        "applicant_civil_number",
        "applicant_birth_date",
        "applicant_nationalities",
        "applicant_identity_source",
    )

    def setUp(self):
        super().setUp()
        applicant = self.proposal.created_by
        applicant.civil_number = "12345"
        applicant.birth_date = date(1990, 1, 1)
        applicant.save()
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call,
            expose_civil_number=False,
            expose_birth_date=False,
            expose_nationalities=False,
            expose_identity_source=False,
        )
        self.batch = self._create_batch(
            status=AssignmentBatchStatuses.SENT,
            sent_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.item.accept()
        self.url = factories.ProposalFactory.get_url(self.proposal)

    def _assert_hidden(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        for field in self.HIDDEN_FIELDS:
            self.assertNotIn(field, response.data)
        self.assertIn("applicant_email", response.data)

    def test_review_path_reviewer_sees_only_exposed_fields(self):
        self._assert_hidden(self.reviewer_user)

    def test_call_reviewer_sees_only_exposed_fields(self):
        call_reviewer = structure_factories.UserFactory()
        self.call.add_user(call_reviewer, CallRole.REVIEWER)
        self._assert_hidden(call_reviewer)

    def test_call_manager_sees_unfiltered_applicant(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["applicant_civil_number"], "12345")

    def test_review_path_reviewer_sees_team_without_role_expiration(self):
        self.proposal.add_user(
            structure_factories.UserFactory(),
            ProposalRole.MEMBER,
            expiration_time=timezone.now() + timedelta(days=30),
        )
        self.client.force_authenticate(self.reviewer_user)

        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="list_users")
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        results = (
            response.data["results"]
            if isinstance(response.data, dict)
            else response.data
        )
        self.assertTrue(results)
        self.assertFalse(any("expiration_time" in item for item in results))
