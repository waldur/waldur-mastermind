"""Backend read-only enforcement for archived calls.

An archived call must reject every mutation across its edit surface: the call
itself, nested offerings / resource templates / workflow steps, role mappings,
rounds and documents. Draft and active calls stay fully editable. These tests
lock in that contract and the ``has_proposals`` gate the frontend relies on.
"""

import uuid
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test
from rest_framework.exceptions import PermissionDenied

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.enums import ChecklistTypes, QuestionTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.fixtures import ProjectRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import (
    archive,
    models,
    step_discussions,
    tasks,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    CallStates,
    ProposalRoleEligibility,
    ProposalStates,
    ReviewerPoolInvitationStatuses,
    StepContributionStatuses,
    TransitionModes,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories, fixtures

ARCHIVED = str(models.CALL_ARCHIVED_MESSAGE)
PARKED_DETAIL = "Step completed. The next stage waits for the call to be active again."
NOTIFY = "waldur_mastermind.proposal.tasks.notify_proposal_decision"


class ArchivedCallEnforcementTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.manager = self.fixture.call_manager
        self.client.force_authenticate(self.manager)

    def archive(self):
        self.call.state = CallStates.ARCHIVED
        self.call.save()

    # --- core call update -------------------------------------------------

    def test_update_call_allowed_when_active(self):
        url = factories.CallFactory.get_protected_url(self.call)
        response = self.client.patch(url, {"description": "new description"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.call.refresh_from_db()
        self.assertEqual(self.call.description, "new description")

    def test_update_call_rejected_when_archived(self):
        self.archive()
        url = factories.CallFactory.get_protected_url(self.call)
        response = self.client.patch(url, {"description": "new description"})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    def test_update_visibility_rejected_when_archived(self):
        self.archive()
        url = factories.CallFactory.get_protected_url(self.call)
        response = self.client.patch(url, {"reviews_visible_to_submitters": True})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    # --- offerings --------------------------------------------------------

    def test_add_offering_rejected_when_archived(self):
        self.archive()
        url = factories.RequestedOfferingFactory.get_list_url(self.call)
        payload = {
            "offering": marketplace_factories.OfferingFactory.get_public_url(
                self.fixture.offering
            ),
        }
        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_delete_offering_rejected_when_archived(self):
        requested_offering = self.fixture.requested_offering
        self.archive()
        url = factories.RequestedOfferingFactory.get_url(self.call, requested_offering)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    # --- resource templates ----------------------------------------------

    def test_add_resource_template_rejected_when_archived(self):
        requested_offering = self.fixture.requested_offering_accepted
        self.archive()
        url = factories.CallResourceTemplateFactory.get_list_url(self.call)
        payload = {
            "name": "Standard VM Template",
            "requested_offering": factories.RequestedOfferingFactory.get_url(
                call=self.call, requested_offering=requested_offering
            ),
            "attributes": {"cpu": 2, "ram": 4096},
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_delete_resource_template_rejected_when_archived(self):
        template = factories.CallResourceTemplateFactory(
            call=self.call,
            requested_offering=self.fixture.requested_offering_accepted,
        )
        self.archive()
        url = factories.CallResourceTemplateFactory.get_url(self.call, template)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    # --- workflow steps ---------------------------------------------------

    def test_add_workflow_step_rejected_when_archived(self):
        self.archive()
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {"step": "administrative_check", "is_enabled": True}
        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_delete_workflow_step_rejected_when_archived(self):
        step = factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check"
        )
        self.archive()
        url = factories.CallWorkflowStepFactory.get_url(self.call, step)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    # --- role mappings ----------------------------------------------------

    def test_add_role_mapping_rejected_when_archived(self):
        self.call.proposalprojectrolemapping_set.all().delete()
        self.archive()
        url = factories.ProposalProjectRoleMappingFactory.get_list_url()
        payload = {
            "call": factories.CallFactory.get_protected_url(self.call),
            "project_role": ProjectRole.MEMBER.name,
            "proposal_role": ProposalRole.MEMBER.name,
        }
        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_delete_role_mapping_rejected_when_archived(self):
        self.call.proposalprojectrolemapping_set.all().delete()
        mapping = factories.ProposalProjectRoleMappingFactory(
            call=self.call,
            proposal_role=ProposalRole.MEMBER,
            project_role=ProjectRole.MEMBER,
        )
        self.archive()
        url = factories.ProposalProjectRoleMappingFactory.get_url(mapping)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    # --- rounds and documents --------------------------------------------

    def test_add_round_rejected_when_archived(self):
        self.archive()
        url = factories.RoundFactory.get_list_url(self.call)
        payload = {
            "start_time": "2030-01-01T00:00",
            "cutoff_time": "2030-01-10T00:00",
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    def test_attach_documents_rejected_when_archived(self):
        self.archive()
        url = factories.CallFactory.get_protected_url(
            self.call, action="attach_documents"
        )
        payload = {"documents": [{"file": dummy_image()}]}
        response = self.client.post(url, payload, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)


class HasProposalsFieldTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.staff = self.fixture.staff
        self.client.force_authenticate(self.staff)

    def test_has_proposals_true_when_proposal_exists(self):
        # The fixture seeds a proposal on ``call`` via its round.
        self.assertTrue(
            models.Proposal.objects.filter(round__call=self.fixture.call).exists()
        )
        url = factories.CallFactory.get_protected_url(self.fixture.call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["has_proposals"])

    def test_has_proposals_false_when_no_proposal(self):
        # ``new_call`` has no rounds and therefore no proposals.
        self.assertFalse(
            models.Proposal.objects.filter(round__call=self.fixture.new_call).exists()
        )
        url = factories.CallFactory.get_protected_url(self.fixture.new_call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["has_proposals"])


class ArchivedCallApplicationTest(test.APITestCase):
    """An archived call stays listed for its history but takes no proposals.

    Covers both shapes from github.com/waldur/waldur-homeport/issues/130: an archived
    call with no rounds, and an archived call whose round is otherwise open.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.round = self.fixture.round
        self.empty_call = factories.CallFactory(
            manager=self.fixture.manager, state=CallStates.ARCHIVED
        )

    def archive(self):
        self.call.state = CallStates.ARCHIVED
        self.call.save()

    def list_public_calls(self, **params):
        response = self.client.get(factories.CallFactory.get_public_list_url(), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {item["uuid"] for item in response.data}

    def test_archived_call_without_rounds_is_listed_but_not_open(self):
        self.assertIn(self.empty_call.uuid.hex, self.list_public_calls())
        self.assertNotIn(
            self.empty_call.uuid.hex, self.list_public_calls(has_active_round=True)
        )

    def test_archived_call_with_open_round_is_listed_but_not_open(self):
        self.assertIn(self.call.uuid.hex, self.list_public_calls(has_active_round=True))
        self.archive()
        self.assertIn(self.call.uuid.hex, self.list_public_calls())
        self.assertNotIn(
            self.call.uuid.hex, self.list_public_calls(has_active_round=True)
        )

    def test_proposal_can_not_be_created_in_archived_call_with_open_round(self):
        self.archive()
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(
            factories.ProposalFactory.get_list_url(),
            {"name": "late", "round_uuid": self.round.uuid.hex},
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertFalse(models.Proposal.objects.filter(name="late").exists())

    def test_draft_can_not_be_submitted_after_call_is_archived(self):
        """Archiving leaves drafts behind; their round may still be open."""
        proposal = self.fixture.proposal
        proposal.state = ProposalStates.DRAFT
        proposal.save()
        proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        self.archive()

        self.client.force_authenticate(self.fixture.proposal_creator)
        response = self.client.post(
            factories.ProposalFactory.get_url(proposal, "submit")
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["detail"], ARCHIVED)
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.DRAFT)

    def test_draft_reports_it_can_not_be_submitted_after_call_is_archived(self):
        """The form disables Submit from ``can_submit`` instead of a refusal."""
        proposal = self.fixture.proposal
        proposal.state = ProposalStates.DRAFT
        proposal.save()
        self.archive()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.ProposalFactory.get_url(proposal))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["can_submit"],
            {"can_submit": False, "error": str(models.CALL_NOT_ACTIVE_MESSAGE)},
        )


# Every write route of a proposal; the guard refuses before the payload is
# read, so none needs one.
PROPOSAL_WRITE_ACTIONS = (
    # workflow steps
    "reopen_decision",
    # changes asked of the applicant
    # contributions and checklists
    "submit_answers",
    # team and roles
    "add_user",
    "update_user",
    "delete_user",
    "role_assignments",
    "revoke_role_assignment",
    "set_role_lead",
    "my_reviewer_suggestions",
    "withdraw_reviewer_suggestion",
    "invite_suggested_reviewers",
    "discard_suggested_reviewers",
    # the proposal itself
    "attach_document",
    "detach_documents",
    "update_project_details",
    "set_panel",
    "resources",
    "submit",
    "withdraw",
    # award and runs
    "awarded_resources",
    "set_awarded_duration",
)

ROUND_WRITE_ACTIONS = (
    "close",
    "start_deciding",
    "publish_results",
    "complete",
    "record_adoption",
    "complete_workflow_step",
    "open_workflow_step",
    "advance_workflow_step",
    "set_workflow_step_due_date",
    "allocate_in_submission_order",
)


class ArchivedCallProposalsBase(test.APITestCase):
    """A proposal in review at an administrative check, followed by the
    allocation decision, with a review being written."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        fixtures.author_as_manager(self.proposal)
        factories.CallWorkflowStepFactory(call=self.call, step="administrative_check")
        factories.CallWorkflowStepFactory(call=self.call, step="allocation_decision")
        self.check = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.PENDING,
        )
        self.proposal.refresh_from_db()
        self.review = factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.fixture.reviewer_1,
            state=models.Review.States.IN_REVIEW,
        )

    def archive(self):
        self.call.state = CallStates.ARCHIVED
        self.call.save()

    def assert_archived(self, response):
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["detail"], ARCHIVED)

    def complete_check(self, user):
        self.client.force_authenticate(user)
        with mock.patch(NOTIFY):
            return self.client.post(
                factories.ProposalFactory.get_url(
                    self.proposal, "complete_workflow_step"
                ),
                {"step_uuid": self.check.uuid.hex, "outcome": "eligible"},
            )


class ArchivedCallProposalWritesTest(ArchivedCallProposalsBase):
    def setUp(self):
        super().setUp()
        self.archive()

    def test_every_proposal_write_action_is_refused_even_for_staff(self):
        self.client.force_authenticate(self.fixture.staff)
        for action in PROPOSAL_WRITE_ACTIONS:
            with self.subTest(action=action):
                self.assert_archived(
                    self.client.post(
                        factories.ProposalFactory.get_url(self.proposal, action), {}
                    )
                )

    def test_writes_to_items_of_a_proposal_are_refused(self):
        self.client.force_authenticate(self.fixture.staff)
        kwargs = {"uuid": self.proposal.uuid.hex, "obj_uuid": uuid.uuid4().hex}
        for name, method in (
            ("proposal-proposal-resource-detail", "patch"),
            ("proposal-proposal-resource-detail", "delete"),
            ("proposal-proposal-awarded_resource-detail", "patch"),
            ("proposal-proposal-awarded_resource-detail", "delete"),
            ("proposal-proposal-resource-purchase-order", "post"),
            ("proposal-proposal-resource-purchase-order", "delete"),
        ):
            with self.subTest(route=name, method=method):
                url = "http://testserver" + reverse(name, kwargs=kwargs)
                self.assert_archived(getattr(self.client, method)(url, {}))

    def test_draft_can_not_be_deleted(self):
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        self.client.force_authenticate(self.fixture.staff)
        self.assert_archived(
            self.client.delete(factories.ProposalFactory.get_url(self.proposal))
        )
        self.assertTrue(models.Proposal.objects.filter(pk=self.proposal.pk).exists())

    def test_call_manager_can_not_complete_a_step(self):
        self.assert_archived(self.complete_check(self.fixture.call_manager))
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

    def test_call_manager_can_not_reject_at_a_step(self):
        self.client.force_authenticate(self.fixture.call_manager)
        self.assert_archived(
            self.client.post(
                factories.ProposalFactory.get_url(
                    self.proposal, "reject_workflow_step"
                ),
                {"step_uuid": self.check.uuid.hex, "reason": "Not eligible"},
            )
        )
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

    def test_applicant_can_not_attach_a_document_to_a_draft(self):
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        self.client.force_authenticate(self.proposal.created_by)
        self.assert_archived(
            self.client.post(
                factories.ProposalFactory.get_url(self.proposal, "attach_document"),
                {"file": dummy_image()},
                format="multipart",
            )
        )
        self.assertFalse(self.proposal.proposaldocumentation_set.exists())

    def test_applicant_can_not_change_the_team(self):
        self.client.force_authenticate(self.proposal.created_by)
        self.assert_archived(
            self.client.post(
                factories.ProposalFactory.get_url(self.proposal, "add_user"),
                {
                    "user": self.fixture.user.uuid.hex,
                    "role": ProposalRole.MEMBER.name,
                },
            )
        )

    def test_reviewer_can_not_submit_a_review(self):
        self.client.force_authenticate(self.fixture.reviewer_1)
        self.assert_archived(
            self.client.post(
                factories.ReviewFactory.get_url(self.review, "submit"),
                {"summary_score": 5},
            )
        )
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.IN_REVIEW)

    def test_every_review_write_is_refused_even_for_staff(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ReviewFactory.get_url(self.review)
        for action in ("submit", "reject"):
            with self.subTest(action=action):
                self.assert_archived(
                    self.client.post(
                        factories.ReviewFactory.get_url(self.review, action), {}
                    )
                )
        self.assert_archived(self.client.patch(url, {"summary_public_comment": "x"}))
        self.assert_archived(self.client.delete(url))

    def test_no_reviewer_is_added_to_a_proposal(self):
        self.client.force_authenticate(self.fixture.call_manager)
        self.assert_archived(
            self.client.post(
                factories.ReviewFactory.get_list_url(),
                {
                    "proposal": factories.ProposalFactory.get_url(self.proposal),
                    "reviewer": structure_factories.UserFactory.get_url(
                        self.fixture.reviewer_2
                    ),
                },
            )
        )
        for action in (
            "create-manual-assignment",
            "generate-assignments",
            "send-all-assignments",
            "import-assignments",
        ):
            with self.subTest(action=action):
                self.assert_archived(
                    self.client.post(
                        factories.CallFactory.get_protected_url(self.call, action), {}
                    )
                )

    def test_no_evaluation_step_is_decided_in_bulk(self):
        self.client.force_authenticate(self.fixture.staff)
        self.assert_archived(
            self.client.post(
                factories.CallFactory.get_protected_url(
                    self.call, "decide_workflow_steps"
                ),
                {
                    "call_step_uuid": self.check.call_step.uuid.hex,
                    "outcome": "eligible",
                    "proposal_uuids": [self.proposal.uuid.hex],
                },
                format="json",
            )
        )
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, WorkflowStepInstanceStatuses.ACTIVE)

    def test_every_round_action_is_refused_even_for_staff(self):
        self.client.force_authenticate(self.fixture.staff)
        kwargs = {"uuid": self.call.uuid.hex, "obj_uuid": self.fixture.round.uuid.hex}
        for action in ROUND_WRITE_ACTIONS:
            with self.subTest(action=action):
                url = "http://testserver" + reverse(
                    "proposal-call-close_round"
                    if action == "close"
                    else f"proposal-call-{action}_round",
                    kwargs=kwargs,
                )
                self.assert_archived(self.client.post(url, {}))


class ArchivedCallProposalReadsTest(ArchivedCallProposalsBase):
    """Reads stay open on an archived call, for the same users as before."""

    def setUp(self):
        super().setUp()
        self.archive()

    def get(self, user, url):
        self.client.force_authenticate(user)
        response = self.client.get(url)
        self.assertEqual(
            response.status_code, status.HTTP_200_OK, getattr(response, "data", None)
        )
        return response

    def test_proposal_and_its_work_are_readable(self):
        for user in (
            self.fixture.staff,
            self.fixture.call_manager,
            self.proposal.created_by,
        ):
            for action in (None, "workflow_states", "contributions", "resources"):
                with self.subTest(user=user.username, action=action):
                    self.get(
                        user, factories.ProposalFactory.get_url(self.proposal, action)
                    )

    def test_reviews_are_readable(self):
        for user in (self.fixture.call_manager, self.fixture.reviewer_1):
            with self.subTest(user=user.username):
                self.get(user, factories.ReviewFactory.get_url(self.review))
                response = self.get(
                    user,
                    factories.ReviewFactory.get_list_url()
                    + f"?proposal_uuid={self.proposal.uuid.hex}",
                )
                self.assertIn(
                    self.review.uuid.hex, [item["uuid"] for item in response.data]
                )

    def test_exports_are_readable(self):
        for action in ("export-proposals", "export-reviews"):
            with self.subTest(action=action):
                self.get(
                    self.fixture.call_manager,
                    factories.CallFactory.get_protected_url(self.call, action),
                )

    def test_a_preview_writes_nothing_and_is_not_refused(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.post(
            factories.ProposalFactory.get_url(self.proposal, "preview-as"), {}
        )
        self.assertNotEqual(
            response.status_code, status.HTTP_409_CONFLICT, response.data
        )

    def test_a_stranger_is_not_told_the_call_is_archived(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(
            factories.ProposalFactory.get_url(self.proposal, "complete_workflow_step"),
            {"step_uuid": self.check.uuid.hex, "outcome": "eligible"},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ArchivedCallReactivationTest(ArchivedCallProposalsBase):
    def test_activating_the_call_lifts_the_guard(self):
        self.archive()
        self.assert_archived(self.complete_check(self.fixture.call_manager))

        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.post(
            factories.CallFactory.get_protected_url(self.call, "activate")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        response = self.complete_check(self.fixture.call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, WorkflowStepInstanceStatuses.COMPLETED)


class ArchivedCallMyTasksTest(ArchivedCallProposalsBase):
    """An archived call asks nothing more of anyone: its work leaves My tasks."""

    def task_proposals(self, user):
        self.client.force_authenticate(user)
        response = self.client.get("http://testserver/api/proposal-my-tasks/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {task["proposal_uuid"] for task in response.data}

    def test_work_of_an_archived_call_leaves_my_tasks(self):
        for user in (self.fixture.call_manager, self.fixture.reviewer_1):
            with self.subTest(user=user.username):
                self.assertIn(self.proposal.uuid.hex, self.task_proposals(user))
        self.archive()
        for user in (self.fixture.call_manager, self.fixture.reviewer_1):
            with self.subTest(user=user.username):
                self.assertNotIn(self.proposal.uuid.hex, self.task_proposals(user))


class ArchivedCallPeriodicTasksTest(ArchivedCallProposalsBase):
    """The periodic tasks leave an archived call's proposals as they were."""

    def test_overdue_step_of_an_archived_call_does_not_expire(self):
        self.check.deadline = timezone.now() - timedelta(hours=1)
        self.check.save()
        self.archive()

        with mock.patch(NOTIFY):
            self.assertEqual(tasks.mark_expired_workflow_steps(), 0)

        self.check.refresh_from_db()
        self.assertEqual(self.check.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

        # Re-activated, the call's overdue step lapses as any other.
        self.call.state = CallStates.ACTIVE
        self.call.save()
        with mock.patch(NOTIFY):
            self.assertEqual(tasks.mark_expired_workflow_steps(), 1)
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, WorkflowStepInstanceStatuses.EXPIRED)

    def test_overdue_review_of_an_archived_call_is_not_cancelled(self):
        with mock.patch.object(
            models.Review,
            "review_end_date",
            new=property(lambda review: timezone.now() - timedelta(days=1)),
        ):
            self.archive()
            tasks.expired_reviews_should_be_cancelled()
            self.review.refresh_from_db()
            self.assertEqual(self.review.state, models.Review.States.IN_REVIEW)

            self.call.state = CallStates.ACTIVE
            self.call.save()
            tasks.expired_reviews_should_be_cancelled()
            self.review.refresh_from_db()
            self.assertEqual(self.review.state, models.Review.States.REJECTED)


class ArchivedCallOtherWritesTest(ArchivedCallProposalsBase):
    """Every other way into an archived call's proposals is closed too:
    reviewer assignments, evaluator roles, conflicts of interest, reviewer
    suggestions and bids, the offerings' teams, the compliance review."""

    def setUp(self):
        super().setUp()
        pool = factories.CallReviewerPoolFactory(call=self.call)
        self.batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=pool,
            status=AssignmentBatchStatuses.SENT,
            expires_at=timezone.now() + timedelta(days=3),
        )
        self.item = factories.AssignmentItemFactory(
            batch=self.batch, proposal=self.proposal
        )
        self.reviewer_profile = pool.reviewer
        role = models.CallProposalRole.objects.create(
            call=self.call,
            key="rapporteur",
            name="Rapporteur",
            eligible_from=ProposalRoleEligibility.CALL_REVIEWER,
            min_holders=0,
            max_holders=1,
        )
        self.holder = self.fixture.reviewer_2
        self.role_assignment = models.ProposalRoleAssignment.objects.create(
            proposal=self.proposal, role=role, user=self.holder
        )
        self.coi = factories.ConflictOfInterestFactory(proposal=self.proposal)
        self.suggestion = models.ReviewerSuggestion.objects.create(
            call=self.call,
            reviewer=factories.ReviewerProfileFactory(),
            affinity_score=0.8,
        )
        self.requested_offering = self.fixture.requested_offering
        self.archive()

    def post(self, user, url, data=None):
        self.client.force_authenticate(user)
        return self.client.post(url, data or {}, format="json")

    def url(self, name, obj, action=None):
        url = "http://testserver" + reverse(name, kwargs={"uuid": obj.uuid.hex})
        return url if action is None else url + action + "/"

    def test_reviewer_assignments_are_refused(self):
        staff = self.fixture.staff
        for action in ("accept", "decline", "reassign", "force-unblock"):
            with self.subTest(item_action=action):
                self.assert_archived(
                    self.post(
                        staff, self.url("assignment-item-detail", self.item, action)
                    )
                )
        self.client.force_authenticate(staff)
        self.assert_archived(
            self.client.delete(self.url("assignment-item-detail", self.item))
        )
        for action in ("send", "cancel", "extend-deadline"):
            with self.subTest(batch_action=action):
                self.assert_archived(
                    self.post(
                        staff, self.url("assignment-batch-detail", self.batch, action)
                    )
                )
        self.client.force_authenticate(staff)
        self.assert_archived(
            self.client.delete(self.url("assignment-batch-detail", self.batch))
        )
        self.assertFalse(
            models.Review.objects.filter(assignment_item=self.item).exists()
        )

    def test_accepting_an_assignment_creates_no_review_whatever_the_path(self):
        with self.assertRaises(IncorrectStateException):
            self.item.accept(self.reviewer_profile.user)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, AssignmentItemStatuses.PENDING)

    def test_confirming_no_conflict_is_refused(self):
        self.assert_archived(
            self.post(
                self.holder,
                self.url(
                    "proposal-role-assignment-detail",
                    self.role_assignment,
                    "confirm_no_conflict",
                ),
            )
        )

    def test_conflicts_of_interest_are_not_resolved(self):
        for action in ("recuse", "waive", "dismiss"):
            with self.subTest(action=action):
                self.assert_archived(
                    self.post(
                        self.fixture.staff,
                        factories.ConflictOfInterestFactory.get_url(self.coi, action),
                    )
                )

    def test_reviewer_suggestions_and_bids_are_refused(self):
        for action in ("confirm", "reject"):
            with self.subTest(action=action):
                self.assert_archived(
                    self.post(
                        self.fixture.staff,
                        self.url("reviewer-suggestion-detail", self.suggestion, action),
                    )
                )
        response = self.post(
            self.reviewer_profile.user,
            "http://testserver" + reverse("reviewer-bid-list") + "submit/",
            {"proposal_uuid": self.proposal.uuid.hex, "bid": "eager"},
        )
        self.assert_archived(response)

    def test_the_offerings_team_changes_nothing(self):
        for action in ("accept", "cancel", "set_default_delegate"):
            with self.subTest(action=action):
                self.assert_archived(
                    self.post(
                        self.fixture.staff,
                        self.url(
                            "proposal-requested-offering-detail",
                            self.requested_offering,
                            action,
                        ),
                    )
                )

    def test_the_compliance_review_is_refused(self):
        self.assert_archived(
            self.post(
                self.fixture.staff,
                factories.CallFactory.get_protected_url(
                    self.call, "review_proposal_compliance"
                ),
            )
        )

    def test_reads_stay_open(self):
        self.client.force_authenticate(self.fixture.staff)
        for name, obj in (
            ("assignment-item-detail", self.item),
            ("assignment-batch-detail", self.batch),
            ("reviewer-suggestion-detail", self.suggestion),
            ("proposal-requested-offering-detail", self.requested_offering),
        ):
            with self.subTest(route=name):
                response = self.client.get(self.url(name, obj))
                self.assertEqual(response.status_code, status.HTTP_200_OK)


class ArchivedCallServicesTest(ArchivedCallProposalsBase):
    """The engine itself stands an archived call's steps still, whatever
    path reaches it."""

    def setUp(self):
        super().setUp()
        self.archive()

    def test_completing_or_rejecting_a_step_is_refused(self):
        with self.assertRaises(IncorrectStateException):
            workflow_service.complete_step(
                self.proposal, self.check, "eligible", "", self.fixture.staff
            )
        with self.assertRaises(IncorrectStateException):
            workflow_service.reject_at_step(
                self.proposal, self.check, "no", self.fixture.staff
            )
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, WorkflowStepInstanceStatuses.ACTIVE)

    def test_contributions_complete_no_step(self):
        self.assertEqual(
            workflow_service.complete_from_contributions(
                self.proposal, self.check, self.fixture.staff
            ),
            (False, None),
        )
        self.assertEqual(
            workflow_service.settle_evaluator_slots(
                self.proposal, self.fixture.reviewer_1, self.fixture.staff
            ),
            (False, None),
        )


class ArchivedCallAwardStageTest(ArchivedCallProposalsBase):
    """The award stage goes on in an archived call; the evaluation, editing
    the award included, stays closed."""

    def setUp(self):
        super().setUp()
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()
        factories.CallWorkflowStepFactory(call=self.call, step="award_response")
        self.response_step = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="award_response",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.check.status = WorkflowStepInstanceStatuses.COMPLETED
        self.check.save()
        self.archive()

    def test_award_stage_actions_are_not_refused_for_the_call(self):
        self.client.force_authenticate(self.proposal.created_by)
        for action in ("request_extension", "complete_workflow_step"):
            with self.subTest(action=action):
                response = self.client.post(
                    factories.ProposalFactory.get_url(self.proposal, action),
                    {"step_uuid": self.response_step.uuid.hex, "outcome": "x"},
                )
                # Refused, if at all, for its own reasons, not the call's.
                self.assertNotEqual(
                    getattr(response, "data", {}).get("detail"), ARCHIVED, response.data
                )
        self.assertFalse(archive.freezes_step(self.proposal, self.response_step))

    def test_editing_the_award_stays_frozen(self):
        self.client.force_authenticate(self.fixture.staff)
        for action in ("set_awarded_duration", "awarded_resources", "attach_document"):
            with self.subTest(action=action):
                self.assert_archived(
                    self.client.post(
                        factories.ProposalFactory.get_url(self.proposal, action), {}
                    )
                )

    def test_evaluation_steps_stay_frozen(self):
        # Back at the evaluation: its step actions are refused at the door,
        # and the engine refuses them whatever the path.
        self.response_step.status = WorkflowStepInstanceStatuses.PENDING
        self.response_step.save()
        self.check.status = WorkflowStepInstanceStatuses.ACTIVE
        self.check.save()
        self.client.force_authenticate(self.fixture.staff)
        for action in ("complete_workflow_step", "submit_contribution"):
            with self.subTest(action=action):
                response = self.client.post(
                    factories.ProposalFactory.get_url(self.proposal, action),
                    {"step_uuid": self.check.uuid.hex, "outcome": "eligible"},
                )
                # Refused: for the call, or for the step; never done.
                self.assertIn(
                    response.status_code,
                    (status.HTTP_409_CONFLICT, status.HTTP_400_BAD_REQUEST),
                    response.data,
                )
        with self.assertRaises(IncorrectStateException):
            workflow_service.complete_step(
                self.proposal, self.check, "eligible", "", self.fixture.staff
            )

    def test_an_overdue_award_stage_step_lapses_and_an_evaluation_one_does_not(self):
        self.response_step.deadline = timezone.now() - timedelta(hours=1)
        self.response_step.save()
        self.assertEqual(
            list(
                models.ProposalWorkflowStepInstance.objects.filter(
                    pk__in=[self.response_step.pk, self.check.pk]
                )
                .exclude(archive.frozen_steps_q())
                .values_list("pk", flat=True)
            ),
            [self.response_step.pk],
        )


class ArchivedCallAssignmentTasksTest(ArchivedCallProposalsBase):
    def setUp(self):
        super().setUp()
        self.batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=factories.CallReviewerPoolFactory(call=self.call),
            status=AssignmentBatchStatuses.SENT,
            sent_at=timezone.now() - timedelta(days=5),
            expires_at=timezone.now() - timedelta(hours=1),
        )
        self.archive()

    def test_an_expired_batch_is_left_as_it_was(self):
        tasks.mark_expired_assignment_batches()
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, AssignmentBatchStatuses.SENT)

    def test_no_reminder_and_no_manager_notice(self):
        self.batch.expires_at = timezone.now() + timedelta(hours=12)
        self.batch.save()
        tasks.send_assignment_expiry_reminders()
        self.batch.refresh_from_db()
        self.assertFalse(self.batch.reminder_sent)

        self.batch.status = AssignmentBatchStatuses.EXPIRED
        self.batch.save()
        tasks.notify_managers_of_expired_batches()
        self.batch.refresh_from_db()
        self.assertFalse(self.batch.manager_notified)


class ArchivedCallStaleDiscussionsTest(ArchivedCallProposalsBase):
    def test_a_stale_discussion_of_an_archived_call_is_left_open(self):
        self.check.status = WorkflowStepInstanceStatuses.COMPLETED
        self.check.save()
        discussion = models.StepDiscussion.objects.create(proposal_step=self.check)
        self.assertIn(discussion.pk, list(step_discussions.stale_ids()))
        self.archive()
        self.assertNotIn(discussion.pk, list(step_discussions.stale_ids()))


class ArchivedCallRefusalOrderTest(ArchivedCallProposalsBase):
    """Whoever could not act anyway is told why as before: the archived call
    is said only to those it stops."""

    def setUp(self):
        super().setUp()
        self.archive()

    def test_a_stranger_opening_a_discussion_is_refused_as_a_stranger(self):
        with self.assertRaises(PermissionDenied):
            step_discussions.open_discussion(
                self.fixture.user, proposal_step=self.check
            )

    def test_a_bidder_off_the_pool_is_told_so(self):
        outsider = factories.ReviewerProfileFactory()
        self.client.force_authenticate(outsider.user)
        url = "http://testserver" + reverse("reviewer-bid-list")
        response = self.client.post(
            url + "submit/",
            {"proposal_uuid": self.proposal.uuid.hex, "bid": "eager"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(
            url + "bulk-submit/",
            {"bids": [{"proposal_uuid": self.proposal.uuid.hex, "bid": "eager"}]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("Not in the reviewer pool", response.data["errors"][0])

    def test_a_pool_member_is_told_the_call_is_archived(self):
        member = factories.CallReviewerPoolFactory(call=self.call).reviewer
        self.client.force_authenticate(member.user)
        url = "http://testserver" + reverse("reviewer-bid-list")
        self.assert_archived(
            self.client.post(
                url + "submit/",
                {"proposal_uuid": self.proposal.uuid.hex, "bid": "eager"},
                format="json",
            )
        )
        response = self.client.post(
            url + "bulk-submit/",
            {"bids": [{"proposal_uuid": self.proposal.uuid.hex, "bid": "eager"}]},
            format="json",
        )
        self.assertEqual(response.data["submitted"], 0)
        self.assertIn(
            str(uuid.UUID(self.proposal.uuid.hex)), response.data["errors"][0]
        )


class ArchivedCallPoolInvitationsTest(ArchivedCallProposalsBase):
    def test_an_expired_invitation_of_an_archived_call_stays_pending(self):
        entry = factories.CallReviewerPoolFactory(
            call=self.call,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
            invitation_expires_at=timezone.now() - timedelta(hours=1),
        )
        self.archive()
        self.assertEqual(tasks.mark_expired_reviewer_pool_invitations(), 0)
        entry.refresh_from_db()
        self.assertEqual(
            entry.invitation_status, ReviewerPoolInvitationStatuses.PENDING
        )


class ArchivedCallStepWorkTest(ArchivedCallProposalsBase):
    """Step work names its step: at the award response of an archived call,
    every step action naming an evaluation step is refused, whatever the
    proposal stands at."""

    def setUp(self):
        super().setUp()
        self.fixture.call_manager  # noqa: B018 -- the call's manager exists
        self.checklist = checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.WORKFLOW_STEP
        )
        self.question = checklist_factories.QuestionFactory(
            checklist=self.checklist, question_type=QuestionTypes.TEXT_INPUT
        )
        self.check.call_step.checklist = self.checklist
        self.check.call_step.save(update_fields=["checklist"])
        self.check.status = WorkflowStepInstanceStatuses.COMPLETED
        self.check.outcome = "eligible"
        self.check.save()
        factories.CallWorkflowStepFactory(call=self.call, step="award_response")
        self.response = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="award_response",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.proposal.current_step = self.response
        self.proposal.save(update_fields=["current_step"])
        self.decision = self.proposal.workflow_step_instances.get(
            step="allocation_decision"
        )
        self.submitted = models.StepContribution.objects.create(
            proposal_step=self.check,
            contributor=self.fixture.reviewer_1,
            status=StepContributionStatuses.SUBMITTED,
            submitted_at=timezone.now(),
        )
        self.open_slot = models.StepContribution.objects.create(
            proposal_step=self.decision,
            contributor=self.fixture.reviewer_1,
            delegate=self.fixture.reviewer_2,
            status=StepContributionStatuses.PENDING,
        )
        self.change_request = models.ProposalChangeRequest.objects.create(
            proposal=self.proposal,
            step_instance=self.check,
            requested_by=self.fixture.call_manager,
            message="Fix it",
            unlocked_parts=["project_details"],
        )
        self.archive()

    def post(self, user, action, data, query=""):
        self.client.force_authenticate(user)
        with mock.patch(NOTIFY):
            return self.client.post(
                factories.ProposalFactory.get_url(self.proposal, action) + query,
                data,
                format="json",
            )

    def assert_refused(self, response):
        """Refused: for the call, or -- naming a step that no longer runs --
        for the step; never done."""
        self.assertIn(
            response.status_code,
            (status.HTTP_409_CONFLICT, status.HTTP_400_BAD_REQUEST),
            response.data,
        )

    def test_every_step_action_naming_an_evaluation_step_is_refused(self):
        staff = self.fixture.staff
        manager = self.fixture.call_manager
        cases = (
            (
                staff,
                "complete_workflow_step",
                {"step_uuid": self.check.uuid.hex, "outcome": "eligible"},
                "",
            ),
            (
                staff,
                "reject_workflow_step",
                {"step_uuid": self.check.uuid.hex, "reason": "x"},
                "",
            ),
            (
                staff,
                "submit_contribution",
                {"step_uuid": self.check.uuid.hex, "outcome": "eligible"},
                "",
            ),
            (
                manager,
                "request_changes",
                {
                    "step_uuid": self.check.uuid.hex,
                    "message": "x",
                    "parts": ["project_details"],
                },
                "",
            ),
        )
        for user, action, data, query in cases:
            with self.subTest(action=action):
                self.assert_refused(self.post(user, action, data, query))
        self.check.refresh_from_db()
        self.assertEqual(self.check.outcome, "eligible")

    def test_releasing_an_evaluation_steps_report_is_refused(self):
        self.assert_archived(
            self.post(
                self.fixture.staff,
                "release_step_report",
                {"step_uuid": self.check.call_step.uuid.hex, "text": "Report"},
            )
        )
        self.check.refresh_from_db()
        self.assertIsNone(self.check.released_at)

    def test_answering_an_evaluation_steps_form_is_refused(self):
        for user in (self.fixture.staff, self.fixture.call_manager):
            with self.subTest(user=user.username):
                self.assert_archived(
                    self.post(
                        user,
                        "submit-step-checklist-answers",
                        [{"question_uuid": self.question.uuid.hex, "answer_data": "x"}],
                        "?step=administrative_check",
                    )
                )
        self.assertFalse(
            checklist_models.Answer.objects.filter(question=self.question).exists()
        )

    def test_contribution_actions_on_evaluation_steps_are_refused(self):
        self.assert_archived(
            self.post(
                self.fixture.staff,
                "reopen_contribution",
                {"contribution_uuid": self.submitted.uuid.hex, "message": "Again"},
            )
        )
        self.assert_archived(
            self.post(
                self.fixture.staff,
                "decline_contribution",
                {"contribution_uuid": self.open_slot.uuid.hex, "comment": "No"},
            )
        )
        response = self.post(
            self.fixture.reviewer_2,
            "accept_contribution",
            {
                "contribution_uuid": self.open_slot.uuid.hex,
                "confirm_no_conflict": True,
            },
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_409_CONFLICT, status.HTTP_404_NOT_FOUND),
            response.data,
        )
        self.open_slot.refresh_from_db()
        self.assertEqual(self.open_slot.status, StepContributionStatuses.PENDING)

    def test_delegating_an_evaluation_slot_is_refused(self):
        response = self.post(
            self.fixture.staff,
            "delegate_contribution",
            {"contribution_uuid": self.open_slot.uuid.hex, "user_uuid": None},
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_409_CONFLICT, status.HTTP_403_FORBIDDEN),
            response.data,
        )
        self.open_slot.refresh_from_db()
        self.assertEqual(self.open_slot.delegate, self.fixture.reviewer_2)

    def test_changes_asked_by_an_evaluation_step_are_neither_called_off_nor_submitted(
        self,
    ):
        self.assert_archived(
            self.post(
                self.fixture.call_manager,
                "cancel_change_request",
                {"change_request_uuid": self.change_request.uuid.hex},
            )
        )
        # The applicant has nothing to submit for an evaluation step either.
        self.assertEqual(
            self.post(self.proposal.created_by, "submit_changes", {}).status_code,
            status.HTTP_409_CONFLICT,
        )
        self.change_request.refresh_from_db()
        self.assertIsNone(self.change_request.closed_at)

    def test_the_award_response_itself_goes_on(self):
        self.assertFalse(archive.freezes_step(self.proposal, self.response))


class ArchivedCallParkingTest(ArchivedCallProposalsBase):
    """An evaluation step after the award response is not entered while the
    call is archived; the proposal waits and moves on once it is active."""

    def setUp(self):
        super().setUp()
        self.check.status = WorkflowStepInstanceStatuses.COMPLETED
        self.check.save()
        factories.CallWorkflowStepFactory(call=self.call, step="award_response")
        self.response = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="award_response",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.after = self.proposal.workflow_step_instances.get(
            step="allocation_decision"
        )
        self.response.order, self.response.stage = 10, 10
        self.response.save()
        self.after.order, self.after.stage = 11, 11
        self.after.save()
        self.proposal.current_step = self.response
        self.proposal.save(update_fields=["current_step"])
        self.archive()

    def test_the_next_evaluation_stage_waits_for_the_activation(self):
        with transaction.atomic():
            models.Proposal.objects.select_for_update().get(pk=self.proposal.pk)
            next_instance = workflow_service._advance_to_next(
                self.proposal, self.response
            )
        self.assertIsNone(next_instance)
        self.after.refresh_from_db()
        self.assertEqual(self.after.status, WorkflowStepInstanceStatuses.PENDING)
        self.response.refresh_from_db()
        self.assertIn(
            workflow_service.PARKED_WHILE_ARCHIVED_KEY,
            self.response.sent_notifications,
        )

        self.call.state = CallStates.ACTIVE
        self.call.save()
        resumed = workflow_service.resume_after_archive(self.call)
        self.assertEqual([proposal.pk for proposal, _ in resumed], [self.proposal.pk])
        self.after.refresh_from_db()
        self.assertEqual(self.after.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.response.refresh_from_db()
        self.assertNotIn(
            workflow_service.PARKED_WHILE_ARCHIVED_KEY,
            self.response.sent_notifications,
        )


class ArchivedCallParkedResponsesTest(ArchivedCallParkingTest):
    """Completing or advancing onto a stage the archive keeps waiting decides
    nothing, and says so: no decision mail, the marker once."""

    DECISION = "waldur_mastermind.proposal.tasks.notify_proposal_decision"

    def markers(self):
        self.response.refresh_from_db()
        return self.response.sent_notifications.count(
            workflow_service.PARKED_WHILE_ARCHIVED_KEY
        )

    def complete_response(self):
        self.client.force_authenticate(self.proposal.created_by)
        return self.client.post(
            factories.ProposalFactory.get_url(self.proposal, "complete_workflow_step"),
            {"step_uuid": self.response.uuid.hex, "outcome": "accepted"},
        )

    def test_completing_the_award_response_waits_without_a_decision(self):
        with mock.patch(self.DECISION) as decision:
            response = self.complete_response()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["detail"], PARKED_DETAIL)
        self.assertEqual(response.data["proposal_state"], ProposalStates.IN_REVIEW)
        decision.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.markers(), 1)

    def test_advancing_by_hand_waits_without_a_decision_however_often(self):
        call_step = self.response.call_step
        call_step.transition_mode = TransitionModes.MANUAL
        call_step.save(update_fields=["transition_mode"])
        with mock.patch(self.DECISION) as decision:
            response = self.complete_response()
            self.assertEqual(
                response.data["detail"], "Step completed. Awaiting manual advance."
            )
            self.client.force_authenticate(self.fixture.staff)
            for _ in range(2):
                response = self.client.post(
                    factories.ProposalFactory.get_url(
                        self.proposal, "advance_workflow_step"
                    )
                )
                self.assertEqual(
                    response.status_code, status.HTTP_200_OK, response.data
                )
                self.assertEqual(
                    response.data["detail"],
                    PARKED_DETAIL.replace("Step completed", "Advanced"),
                )
        decision.assert_not_called()
        self.assertEqual(self.markers(), 1)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)


class ArchivedCallResumeScopeTest(ArchivedCallProposalsBase):
    """The resume carries on only what the archive held back."""

    def setUp(self):
        super().setUp()
        self.check.call_step.outcome_set = "per_offering"
        self.check.call_step.save(update_fields=["outcome_set"])
        models.StepContribution.objects.create(
            proposal_step=self.check,
            contributor=self.fixture.reviewer_1,
            status=StepContributionStatuses.SUBMITTED,
            outcome="accepted",
            submitted_at=timezone.now(),
        )

    def resume(self):
        with mock.patch.object(
            workflow_service, "complete_from_contributions", return_value=(True, None)
        ) as complete:
            workflow_service.resume_after_archive()
        return complete

    def test_a_call_never_archived_is_left_to_its_people(self):
        self.resume().assert_not_called()

    def test_a_step_held_back_by_the_archive_is_offered_again(self):
        self.archive()
        with transaction.atomic():
            self.assertEqual(
                workflow_service.complete_from_contributions(
                    self.proposal, self.check, self.fixture.staff
                ),
                (False, None),
            )
        self.check.refresh_from_db()
        self.assertIn(
            workflow_service.HELD_WHILE_ARCHIVED_KEY, self.check.sent_notifications
        )
        self.call.state = CallStates.ACTIVE
        self.call.save()
        complete = self.resume()
        complete.assert_called_once()
        self.check.refresh_from_db()
        self.assertNotIn(
            workflow_service.HELD_WHILE_ARCHIVED_KEY, self.check.sent_notifications
        )


class ArchivedCallActivationResumesTest(ArchivedCallProposalsBase):
    def test_activating_schedules_the_resume(self):
        self.archive()
        with mock.patch(
            "waldur_mastermind.proposal.tasks.resume_work_after_archive.delay"
        ) as resume:
            self.client.force_authenticate(self.fixture.call_manager)
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    factories.CallFactory.get_protected_url(self.call, "activate")
                )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        resume.assert_called_once_with(self.call.uuid.hex)

    def test_the_resume_tells_of_a_decision(self):
        with (
            mock.patch.object(
                workflow_service,
                "resume_after_archive",
                return_value=[(self.proposal, None)],
            ) as resume,
            mock.patch(
                "waldur_mastermind.proposal.tasks.notify_contribution_decision"
            ) as notify,
        ):
            self.assertEqual(tasks.resume_work_after_archive(self.call.uuid.hex), 1)
        self.assertEqual(resume.call_args.args[0].pk, self.call.pk)
        notify.assert_called_once_with(self.proposal, True, None)

    def test_a_failing_proposal_does_not_hold_up_the_rest(self):
        self.archive()
        self.call.state = CallStates.ACTIVE
        self.call.save()
        with mock.patch.object(
            workflow_service,
            "complete_from_contributions",
            side_effect=RuntimeError("boom"),
        ):
            # Nothing to carry on raises nothing either way.
            self.assertEqual(workflow_service.resume_after_archive(self.call), [])
