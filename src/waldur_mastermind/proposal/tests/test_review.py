import datetime
from unittest import mock

from ddt import data, ddt, unpack
from django.core import mail
from django.template.loader import render_to_string
from django.test import override_settings
from django.utils import timezone
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, CustomerRole
from waldur_core.permissions.tests.pat_utils import use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import enums, models, notification_overrides, tasks
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class ReviewGetTest(test.APITestCase):
    def setUp(self):
        # As the deployed roles do (permissions.yaml): an owner of the managing
        # customer decides its calls' proposals, and so reads their reviews.
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_AND_REJECT_PROPOSALS)
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ReviewFactory.get_list_url()

    def _get_review_request(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.get(self.url)

    @data(
        "staff",
        "owner",
    )
    def test_review_should_be_visible(self, user):
        response = self._get_review_request(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    # The organisation's support holds no call permission: no call team.
    @data("user", "proposal_submitted_creator", "reviewer_2", "customer_support")
    def test_review_should_not_be_visible(self, user):
        response = self._get_review_request(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(len(response.json()))

    def test_an_observer_of_the_results_reads_no_review(self):
        # A reader of the managing organisation reads published results, not
        # the reviews behind them: not who wrote one, nor its private comment.
        observer = structure_factories.UserFactory()
        self.fixture.call.manager.customer.add_user(observer, CustomerRole.READER)
        self.fixture.review.state = models.Review.States.SUBMITTED
        self.fixture.review.save()
        self.client.force_authenticate(observer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [])

    @data("proposal_submitted_creator")
    def test_submitted_review_for_decided_proposal_should_be_visible(self, user):
        self.fixture.review.proposal.state = ProposalStates.ACCEPTED
        self.fixture.review.proposal.save()
        self.fixture.review.state = models.Review.States.SUBMITTED
        self.fixture.review.save()
        response = self._get_review_request(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data("proposal_submitted_creator")
    def test_submitted_review_for_undecided_proposal_should_not_be_visible(self, user):
        self.fixture.review.state = models.Review.States.SUBMITTED
        self.fixture.review.save()
        response = self._get_review_request(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(len(response.json()))


@ddt
class ReviewCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ReviewFactory.get_list_url()

    @data("staff", "call_manager", "call_organizer_user")
    def test_user_can_add(self, user):
        response = self.create(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Review.objects.filter(uuid=response.data["uuid"]).exists()
        )

    @data(
        "owner",
        "customer_support",
        "user",
    )
    def test_user_cannot_add(self, user):
        response = self.create(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff", "call_manager")
    def test_user_cannot_add_duplicate_reviews(self, user):
        response = self.create(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Review.objects.filter(uuid=response.data["uuid"]).exists()
        )
        # Try to create the same review again
        response = self.create(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Review already exists for this proposal and reviewer", response.data[0]
        )

    @override_settings(task_always_eager=True)
    @data("staff", "call_manager")
    def test_reviewer_notification_sent_on_create(self, user):
        structure_factories.NotificationFactory(
            key="proposal.review_assigned",
        )

        response = self.create(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.fixture.reviewer_2.email])

        subject = mail.outbox[0].subject
        self.assertIn("New review assignment", subject)

        body = mail.outbox[0].body
        self.assertIn(self.fixture.reviewer_2.full_name, body)
        self.assertIn(self.fixture.proposal_submitted.name, body)
        self.assertIn(self.fixture.call.name, body)

    def create(self, user, **kwargs):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "proposal": factories.ProposalFactory.get_url(
                self.fixture.proposal_submitted
            ),
            "reviewer": structure_factories.UserFactory.get_url(
                self.fixture.reviewer_2
            ),
        }
        payload.update(kwargs)

        return self.client.post(self.url, payload)


@ddt
class ReviewUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.review = self.fixture.review
        self.url = factories.ReviewFactory.get_url(self.review)

    @data("staff", "reviewer_1")
    def test_user_can_update(self, user):
        response = self.update(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("proposal_submitted_creator", "reviewer_2")
    def test_user_can_not_update(self, user):
        response = self.update(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def update(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "summary_score": 5,
        }
        response = self.client.patch(self.url, payload)
        self.review.refresh_from_db()
        return response


class ReviewDeadlineReminderNotificationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.review = self.fixture.review

        self.review.proposal.round.review_duration_in_days = 3
        self.review.proposal.round.save(update_fields=["review_duration_in_days"])

    @override_settings(task_always_eager=True)
    def test_reviewer_is_notified_when_review_deadline_is_within_three_days(self):
        structure_factories.NotificationFactory(
            key="proposal.review_deadline_approaching",
        )

        tasks.notify_reviewer_on_review_deadline_approaching()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.review.reviewer.email])
        self.assertIn(self.review.proposal.name, mail.outbox[0].subject)
        self.assertIn(self.review.reviewer.full_name, mail.outbox[0].body)
        self.assertIn(self.review.proposal.round.call.name, mail.outbox[0].body)

    @override_settings(task_always_eager=True)
    def test_reviewer_is_not_notified_before_three_day_window(self):
        structure_factories.NotificationFactory(
            key="proposal.review_deadline_approaching",
        )
        self.review.proposal.round.review_duration_in_days = 4
        self.review.proposal.round.save(update_fields=["review_duration_in_days"])

        tasks.notify_reviewer_on_review_deadline_approaching()

        self.assertEqual(len(mail.outbox), 0)

    @override_settings(task_always_eager=True)
    def test_reviewer_is_not_notified_after_review_deadline_has_passed(self):
        structure_factories.NotificationFactory(
            key="proposal.review_deadline_approaching",
        )
        models.Review.objects.filter(pk=self.review.pk).update(
            created=timezone.now() - datetime.timedelta(days=5)
        )
        self.review.refresh_from_db()

        tasks.notify_reviewer_on_review_deadline_approaching()

        self.assertEqual(len(mail.outbox), 0)

    @override_settings(task_always_eager=True)
    def test_submitted_review_is_not_notified(self):
        structure_factories.NotificationFactory(
            key="proposal.review_deadline_approaching",
        )
        self.review.state = models.Review.States.SUBMITTED
        self.review.save(update_fields=["state"])

        tasks.notify_reviewer_on_review_deadline_approaching()

        self.assertEqual(len(mail.outbox), 0)


@ddt
class ReviewDeleteTest(test.APITestCase):
    def setUp(self):
        # As the deployed roles do (permissions.yaml): an owner of the managing
        # customer decides its calls' proposals, and so reads their reviews.
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_AND_REJECT_PROPOSALS)
        self.fixture = fixtures.ProposalFixture()
        self.review = self.fixture.review
        self.url = factories.ReviewFactory.get_url(self.review)

    @data(
        "staff",
    )
    def test_staff_can_delete(self, user):
        response = self.run_delete(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_customer_user_can_not_delete(self):
        response = self.run_delete("owner")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # The organisation's support does not read the review at all.
        response = self.run_delete("customer_support")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def run_delete(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.delete(self.url)


@override_config(PAT_ENABLED=True)
class ReviewTokenTest(test.APITestCase):
    """Assigning and removing a review with a personal access token needs
    PROPOSAL.MANAGE_REVIEW among its scopes and a binding that covers the call."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.manager = self.fixture.call_manager
        self.other_call = factories.CallFactory()
        self.other_call.add_user(self.manager, CallRole.MANAGER)
        self.review = self.fixture.review

    def create(self):
        return self.client.post(
            factories.ReviewFactory.get_list_url(),
            {
                "proposal": factories.ProposalFactory.get_url(
                    self.fixture.proposal_submitted
                ),
                "reviewer": structure_factories.UserFactory.get_url(
                    self.fixture.reviewer_2
                ),
            },
        )

    def delete(self):
        return self.client.delete(factories.ReviewFactory.get_url(self.review))

    def assert_refused(self):
        self.assertEqual(self.create().status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.Review.objects.filter(reviewer=self.fixture.reviewer_2).exists()
        )
        self.assertEqual(self.delete().status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Review.objects.filter(pk=self.review.pk).exists())

    def assert_allowed(self):
        self.assertEqual(self.create().status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.delete().status_code, status.HTTP_204_NO_CONTENT)

    def test_token_without_manage_review_scope_is_refused(self):
        use_pat(self.client, self.manager, PermissionEnum.LIST_CALLS.value)
        self.assert_refused()

    def test_token_bound_to_another_call_is_refused(self):
        use_pat(
            self.client,
            self.manager,
            PermissionEnum.MANAGE_PROPOSAL_REVIEW.value,
            bindings=[self.other_call],
        )
        self.assert_refused()

    def test_token_with_scope_is_allowed(self):
        use_pat(self.client, self.manager, PermissionEnum.MANAGE_PROPOSAL_REVIEW.value)
        self.assert_allowed()

    def test_token_bound_to_the_call_is_allowed(self):
        use_pat(
            self.client,
            self.manager,
            PermissionEnum.MANAGE_PROPOSAL_REVIEW.value,
            bindings=[self.fixture.call],
        )
        self.assert_allowed()

    def test_session_is_allowed(self):
        self.client.force_authenticate(self.manager)
        self.assert_allowed()


@ddt
class ActionTest(test.APITestCase):
    def setUp(self):
        # As the deployed roles do (permissions.yaml): an owner of the managing
        # customer decides its calls' proposals, and so reads their reviews.
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_AND_REJECT_PROPOSALS)
        self.fixture = fixtures.ProposalFixture()
        self.review = self.fixture.review
        self.call_manager = self.fixture.call_manager
        self.review.proposal.round.call.add_user(self.call_manager, CallRole.MANAGER)

    def _submit_review(self, user):
        url = factories.ReviewFactory.get_url(self.review, "submit")
        self.review.state = models.Review.States.IN_REVIEW
        self.review.save()
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(
            url,
            {
                "summary_score": "4",
                "summary_public_comment": "summary public",
                "summary_private_comment": "summary private",
            },
        )
        return response

    @mock.patch(
        "waldur_mastermind.proposal.tasks.notify_call_managers_about_new_review.delay"
    )
    @data(
        "staff",
        "reviewer_1",
    )
    def test_user_can_submit(self, user, mock_notify):
        response = self._submit_review(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertTrue(self.review.state, models.Review.States.SUBMITTED)
        self.assertIsNotNone(self.review.submitted_at)
        self.assertTrue(self.review.summary_score, 4)
        self.assertTrue(self.review.summary_public_comment, "summary public")
        self.assertTrue(self.review.summary_private_comment, "summary private")

        # Verify notification task was called
        mock_notify.assert_called_once_with(self.review.uuid)

    @override_settings(task_always_eager=True)
    @data("reviewer_1")
    def test_notifications_after_submit(self, user):
        structure_factories.NotificationFactory(
            key="proposal.new_review_submitted",
        )
        response = self._submit_review(user)
        user = getattr(self.fixture, user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify notification email was sent
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.call_manager.email])

        subject = mail.outbox[0].subject
        self.assertIn(
            f"Review submitted for proposal: {self.review.proposal.name}", subject
        )

        body = mail.outbox[0].body
        self.assertIn(
            f'A review has been submitted for proposal "{self.review.proposal.name}" in call "{self.review.proposal.round.call.name}".',
            body,
        )
        self.assertIn(user.first_name, body)
        self.assertIn("Review Progress:", body)
        self.assertIn(str(self.review.summary_score), body)

    @data(
        ("owner", status.HTTP_403_FORBIDDEN),
        ("customer_support", status.HTTP_404_NOT_FOUND),
    )
    @unpack
    def test_user_can_not_submit(self, user, expected):
        url = factories.ReviewFactory.get_url(self.review, "submit")
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(url)
        self.assertEqual(response.status_code, expected)

    @override_settings(task_always_eager=True)
    @data("reviewer_1")
    def test_notifications_after_reject(self, user):
        structure_factories.NotificationFactory(
            key="proposal.review_rejected",
        )
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        response = self.client.post(
            factories.ReviewFactory.get_url(self.review, "reject"),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertEqual(
            self.review.rejection_cause, enums.ReviewRejectionCauses.DECLINED
        )

        # Verify notification email was sent
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.call_manager.email])

        subject = mail.outbox[0].subject
        self.assertIn(
            f"Alert: review assignment rejected for {self.review.proposal.name}",
            subject,
        )

        body = mail.outbox[0].body
        self.assertIn(
            f'A reviewer has rejected their assignment to review proposal "{self.review.proposal.name}" in call "{self.review.proposal.round.call.name}".',
            body,
        )
        self.assertIn(user.first_name, body)
        self.assertIn("Review Progress:", body)

    @override_settings(task_always_eager=True)
    @data("reviewer_1")
    def test_reviews_complete_notifications_sent_after_submit(self, user):
        structure_factories.NotificationFactory(
            key="proposal.reviews_complete",
        )

        response = self._submit_review(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.call_manager.email])

        subject = mail.outbox[0].subject
        self.assertEqual(
            f"All reviews complete for proposal: {self.review.proposal.name}", subject
        )

        body = mail.outbox[0].body
        self.assertIn(
            f'All required reviews have been completed for proposal "{self.review.proposal.name}" in call "{self.review.proposal.round.call.name}".',
            body,
        )
        self.assertIn(self.review.proposal.created_by.full_name, body)

    @override_settings(task_always_eager=True)
    @data("reviewer_1")
    def test_reviews_complete_notifications_not_sent(self, user):
        structure_factories.NotificationFactory(
            key="proposal.reviews_complete",
        )

        # if there is an incomplete review
        factories.ReviewFactory(
            proposal=self.review.proposal,
            reviewer=self.fixture.reviewer_2,
            state=models.Review.States.IN_REVIEW,
        )
        response = self._submit_review(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 0)

        # if not minimum number of reviews reached (the reviewer-count gate now
        # lives on the expert_review workflow step, not the Round)
        expert_step = models.CallWorkflowStep.objects.get(
            call=self.review.proposal.round.call, key="expert_review"
        )
        expert_step.min_reviewers = 2
        expert_step.save()

        response = self._submit_review(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(len(mail.outbox), 0)


@ddt
class ReviewerGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.round_2 = fixtures.ProposalFixture().round
        self.url = factories.RoundFactory.get_own_url(
            self.fixture.round, action="reviewers"
        )
        self.url_2 = factories.RoundFactory.get_own_url(
            self.round_2, action="reviewers"
        )

    @data(
        "staff",
    )
    def test_reviewers_counters_are_zero_for_unrelated_proposals(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))
        self.assertEqual(response.data[0]["in_review_proposals"], 0)
        self.assertEqual(response.data[0]["rejected_proposals"], 0)
        self.assertEqual(response.data[0]["accepted_proposals"], 0)

    @data(
        "staff",
    )
    def test_reviewers_counters_are_zero_for_unrelated_rounds(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url_2)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))
        self.assertEqual(response.data[0]["in_review_proposals"], 0)
        self.assertEqual(response.data[0]["rejected_proposals"], 0)
        self.assertEqual(response.data[0]["accepted_proposals"], 0)

    @data(
        "staff",
    )
    def test_reviewers_counter_should_be_visible(self, user):
        self.fixture.proposal.state = ProposalStates.IN_REVIEW
        self.fixture.review.proposal = self.fixture.proposal
        self.fixture.review.save()
        self.fixture.proposal.save()
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))
        self.assertEqual(response.data[0]["in_review_proposals"], 1)


@ddt
class ReviewCoiConfirmationTest(test.APITestCase):
    """requires_coi_confirmation: a reviewer must attest absence of conflict of
    interest before a review can be submitted, but only when the call has an
    enabled workflow step configured with that flag."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.review = self.fixture.review
        self.review.state = models.Review.States.IN_REVIEW
        self.review.save()
        self.call = self.review.proposal.round.call
        self.url = factories.ReviewFactory.get_url(self.review, "submit")

    def _enable_coi_step(self):
        factories.CallWorkflowStepFactory(
            call=self.call,
            step="expert_review",
            is_enabled=True,
            requires_coi_confirmation=True,
        )

    def _submit(self, payload=None):
        self.client.force_authenticate(self.fixture.reviewer_1)
        data = {
            "summary_score": "4",
            "summary_public_comment": "ok",
            "summary_private_comment": "ok",
        }
        if payload:
            data.update(payload)
        with (
            mock.patch(
                "waldur_mastermind.proposal.tasks."
                "notify_call_managers_about_new_review.delay"
            ),
            mock.patch(
                "waldur_mastermind.proposal.tasks."
                "notify_manager_when_reviews_are_completed.delay"
            ),
        ):
            return self.client.post(self.url, data)

    def test_submit_without_coi_step_does_not_require_confirmation(self):
        response = self._submit()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.SUBMITTED)

    def test_submit_blocked_when_coi_required_and_not_confirmed(self):
        self._enable_coi_step()
        response = self._submit()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("coi_confirmed", response.data)
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.IN_REVIEW)

    def test_submit_allowed_when_coi_required_and_confirmed(self):
        self._enable_coi_step()
        response = self._submit({"coi_confirmed": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.SUBMITTED)
        self.assertTrue(self.review.coi_confirmed)
        self.assertIsNotNone(self.review.coi_confirmed_at)

    def test_the_reviewer_confirms_the_calls_declaration_and_keeps_it(self):
        self._enable_coi_step()
        self.call.coi_declaration_text = "No ties to the applicants."
        self.call.save()
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(factories.ReviewFactory.get_url(self.review))
        self.assertEqual(
            response.data["coi_declaration_text"], "No ties to the applicants."
        )
        response = self._submit({"coi_confirmed": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.call.coi_declaration_text = "Another wording."
        self.call.save()
        self.review.refresh_from_db()
        self.assertEqual(self.review.coi_declaration_text, "No ties to the applicants.")
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(factories.ReviewFactory.get_url(self.review))
        self.assertEqual(
            response.data["coi_declaration_text"], "No ties to the applicants."
        )

    def test_a_steps_own_declaration_comes_before_the_calls(self):
        factories.CallWorkflowStepFactory(
            call=self.call,
            step="expert_review",
            is_enabled=True,
            requires_coi_confirmation=True,
            coi_declaration_text="The step's own words.",
        )
        self.call.coi_declaration_text = "The call's words."
        self.call.save()
        self._submit({"coi_confirmed": True})
        self.review.refresh_from_db()
        self.assertEqual(self.review.coi_declaration_text, "The step's own words.")

    def test_disabled_coi_step_does_not_trigger_requirement(self):
        factories.CallWorkflowStepFactory(
            call=self.call,
            step="expert_review",
            is_enabled=False,
            requires_coi_confirmation=True,
        )
        response = self._submit()
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_confirmation_required_flag_exposed_on_review(self):
        self._enable_coi_step()
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(factories.ReviewFactory.get_url(self.review))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["coi_confirmation_required"])

    def test_coi_confirmed_at_cleared_when_resubmitted_unconfirmed(self):
        # The timestamp must not go stale: submitting with coi_confirmed=False
        # (no COI step configured, so it's allowed) clears coi_confirmed_at.
        self.review.coi_confirmed = True
        self.review.coi_confirmed_at = timezone.now()
        self.review.save()
        response = self._submit({"coi_confirmed": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review.refresh_from_db()
        self.assertFalse(self.review.coi_confirmed)
        self.assertIsNone(self.review.coi_confirmed_at)


class ReviewCriteriaScoresTest(test.APITestCase):
    """A review given in a step with criteria is rated per criterion, and its
    summary score is their rounded average."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.review = self.fixture.review
        self.review.state = models.Review.States.IN_REVIEW
        self.review.save()
        step = models.CallWorkflowStep.review_step_for(self.review.proposal.round.call)
        self.assertIsNotNone(step)
        self.relevance = models.WorkflowCriterion.objects.create(
            workflow_step=step, name="Relevance", order=0
        )
        self.feasibility = models.WorkflowCriterion.objects.create(
            workflow_step=step, name="Feasibility", order=1
        )
        self.client.force_authenticate(self.fixture.reviewer_1)

    def _patch(self, payload):
        return self.client.patch(
            factories.ReviewFactory.get_url(self.review), payload, format="json"
        )

    def _submit(self, payload):
        with (
            mock.patch(
                "waldur_mastermind.proposal.tasks."
                "notify_call_managers_about_new_review.delay"
            ),
            mock.patch(
                "waldur_mastermind.proposal.tasks."
                "notify_manager_when_reviews_are_completed.delay"
            ),
        ):
            return self.client.post(
                factories.ReviewFactory.get_url(self.review, "submit"),
                payload,
                format="json",
            )

    def test_review_lists_its_steps_criteria(self):
        response = self.client.get(factories.ReviewFactory.get_url(self.review))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [c["name"] for c in response.data["criteria"]],
            ["Relevance", "Feasibility"],
        )
        self.assertEqual(response.data["criteria"][0]["uuid"], self.relevance.uuid.hex)

    def test_draft_scores_set_the_summary_score(self):
        response = self._patch(
            {
                "criteria_scores": {
                    self.relevance.uuid.hex: 5,
                    self.feasibility.uuid.hex: 2,
                }
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.review.refresh_from_db()
        self.assertEqual(self.review.summary_score, 4)  # 3.5 rounds half up
        self.assertEqual(
            self.review.criteria_scores,
            {self.relevance.uuid.hex: 5, self.feasibility.uuid.hex: 2},
        )

    def test_a_draft_may_leave_criteria_unscored(self):
        response = self._patch({"criteria_scores": {self.relevance.uuid.hex: 3}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_score_of_an_unknown_criterion_is_refused(self):
        response = self._patch({"criteria_scores": {"0" * 32: 3}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_score_out_of_scale_is_refused(self):
        response = self._patch({"criteria_scores": {self.relevance.uuid.hex: 6}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_submit_requires_every_criterion(self):
        response = self._submit({"criteria_scores": {self.relevance.uuid.hex: 4}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Feasibility", str(response.data["criteria_scores"]))

    def test_submit_derives_summary_from_criteria(self):
        response = self._submit(
            {
                "summary_score": 1,
                "criteria_scores": {
                    self.relevance.uuid.hex: 4,
                    self.feasibility.uuid.hex: 5,
                },
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.SUBMITTED)
        self.assertEqual(self.review.summary_score, 5)

    def test_submit_uses_saved_draft_scores(self):
        self.review.criteria_scores = {
            self.relevance.uuid.hex: 3,
            self.feasibility.uuid.hex: 3,
        }
        self.review.save()
        response = self._submit({})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.review.refresh_from_db()
        self.assertEqual(self.review.summary_score, 3)

    def test_step_without_criteria_keeps_the_single_score(self):
        models.WorkflowCriterion.objects.all().delete()
        response = self._submit({"summary_score": 2})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.review.refresh_from_db()
        self.assertEqual(self.review.summary_score, 2)

    def test_criteria_carry_their_weights(self):
        response = self.client.get(factories.ReviewFactory.get_url(self.review))
        self.assertEqual([c["weight"] for c in response.data["criteria"]], [1, 1])

    def test_summary_is_the_weighted_average(self):
        self.relevance.weight = 3
        self.relevance.save()
        response = self._submit(
            {
                "criteria_scores": {
                    self.relevance.uuid.hex: 5,
                    self.feasibility.uuid.hex: 1,
                }
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.review.refresh_from_db()
        self.assertEqual(self.review.summary_score, 4)  # (3*5 + 1) / 4

    def test_weighted_average_rounds_half_up(self):
        self.relevance.weight = 3
        self.relevance.save()
        # (3*4 + 2) / 4 = 3.5
        response = self._patch(
            {
                "criteria_scores": {
                    self.relevance.uuid.hex: 4,
                    self.feasibility.uuid.hex: 2,
                }
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.review.refresh_from_db()
        self.assertEqual(self.review.summary_score, 4)

    def test_single_score_above_the_scale_is_refused(self):
        models.WorkflowCriterion.objects.all().delete()
        response = self._patch({"summary_score": 6})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("summary_score", response.data)


class ReviewCriteriaMailTest(test.APITestCase):
    """The mails to the call managers carry each criterion's score."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.review = self.fixture.review
        call = self.review.proposal.round.call
        # The mails go to the call's managers.
        call.add_user(self.fixture.call_manager, CallRole.MANAGER)
        step = models.CallWorkflowStep.review_step_for(call)
        self.relevance = models.WorkflowCriterion.objects.create(
            workflow_step=step, name="Relevance", order=0, weight=2
        )
        self.feasibility = models.WorkflowCriterion.objects.create(
            workflow_step=step, name="Feasibility", order=1
        )
        self.review.state = models.Review.States.SUBMITTED
        self.review.summary_score = 4
        self.review.criteria_scores = {self.relevance.uuid.hex: 5}
        self.review.save()

    def test_criterion_scores_name_every_criterion_in_order(self):
        self.assertEqual(
            tasks.criterion_scores(self.review),
            [
                {"name": "Relevance", "weight": 2, "score": 5},
                {"name": "Feasibility", "weight": 1, "score": None},
            ],
        )

    def test_new_review_mail_lists_the_criteria(self):
        structure_factories.NotificationFactory(key="proposal.new_review_submitted")
        tasks.notify_call_managers_about_new_review(self.review.uuid)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("- Relevance: 5/5", body)
        self.assertIn("- Feasibility: not rated", body)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn("Relevance: 5/5", html)
        # The submission date is filled in the HTML body too.
        self.assertNotIn("Submission date: <br>", html)

    def test_reviews_complete_mail_lists_each_reviews_criteria(self):
        [reviews_mail] = tasks.reviews_completed_mails(self.review.proposal)
        [entry] = reviews_mail.context["reviews"]
        self.assertEqual(
            [c["name"] for c in entry["criteria"]], ["Relevance", "Feasibility"]
        )
        for template in ("message.txt", "message.html"):
            body = render_to_string(
                f"proposal/reviews_complete_{template}", reviews_mail.context
            )
            self.assertIn("Relevance: 5/5", body)

    def test_an_override_may_loop_over_the_criteria(self):
        mail_type = notification_overrides.get_type("reviews_complete")
        notification_overrides.validate(
            mail_type,
            "Reviews in",
            "{% for r in reviews %}{% for c in r.criteria %}"
            "{{ c.name }}={{ c.score }} {% endfor %}{% endfor %}",
            "",
        )
        [reviews_mail] = tasks.reviews_completed_mails(self.review.proposal)
        _, text, _ = notification_overrides.sandbox_render(
            mail_type,
            "Reviews in",
            "{% for r in reviews %}{% for c in r.criteria %}"
            "{{ c.name }}={{ c.score }} {% endfor %}{% endfor %}",
            "",
            reviews_mail.context,
        )
        self.assertEqual(text, "Relevance=5 Feasibility=None")
