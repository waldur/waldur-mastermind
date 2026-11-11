"""Requesting changes: a proposal sent back to its applicant while it stays in
review, and a submitted contribution reopened for its contributor."""

import datetime
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test
from rest_framework.exceptions import ValidationError

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.core.models import Notification
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.permissions.utils import RoleChange
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    change_requests,
    handlers,
    models,
    my_tasks,
    tasks,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    ChangeRequestParts,
    ChangeRequestResolutions,
    ContributionOutcomes,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    StepContributionStatuses,
    TransitionModes,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_conditional_and_parallel_steps import (
    DISPATCH,
    ParallelStageBase,
)
from waldur_mastermind.proposal.tests.test_step_contributions import (
    PerOfferingAssessmentBase,
)

ACTIVE = WorkflowStepInstanceStatuses.ACTIVE


def run_now(task):
    """Run a queued task in-process: the mail it sends is under test."""
    return mock.patch.object(task, "delay", side_effect=task)


class ChangeRequestBase(test.APITestCase):
    """A proposal in review at an administrative check that allows sending it
    back to its applicant."""

    def setUp(self):
        ProposalRole.MANAGER.add_permission(PermissionEnum.UPDATE_PROPOSAL)
        ProposalRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL)
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call_manager = self.fixture.call_manager
        self.call_step = models.CallWorkflowStep.objects.get(
            call=self.call, key="administrative_check"
        )
        self.call_step.allows_change_requests = True
        self.call_step.duration_in_days = 14
        self.call_step.save()

        self.proposal = factories.ProposalFactory(
            round=self.fixture.round,
            name="Exascale climate runs",
            state=ProposalStates.SUBMITTED,
            project_summary="First summary",
        )
        self.submitted_at = timezone.now() - datetime.timedelta(days=3)
        self.proposal.submitted_at = self.submitted_at
        self.proposal.save()
        self.applicant = fixtures.author_as_manager(self.proposal)
        self.resource = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.fixture.requested_offering_accepted,
        )
        workflow_service.create_step_instances(self.proposal)
        workflow_service.activate_first_step(self.proposal)
        self.proposal.refresh_from_db()
        self.instance = self.proposal.current_step
        self.assertEqual(self.instance.call_step, self.call_step)
        mail.outbox = []

    def _url(self, action):
        return factories.ProposalFactory.get_url(self.proposal, action=action)

    def request_changes(self, user=None, parts=None, **extra):
        self.client.force_authenticate(user or self.call_manager)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                self._url("request_changes"),
                {
                    "step_uuid": self.instance.uuid.hex,
                    "message": "Attach the signed form and shorten the summary.",
                    "parts": parts or ["field:project_summary", "documents"],
                    **extra,
                },
                format="json",
            )

    def submit_changes(self, user=None):
        self.client.force_authenticate(user or self.applicant)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self._url("submit_changes"))

    def update_details(self, user=None, **data):
        # The form sends the name with every save.
        data.setdefault("name", self.proposal.name)
        self.client.force_authenticate(user or self.applicant)
        return self.client.post(
            self._url("update_project_details"), data, format="json"
        )

    def resource_url(self):
        return self._url("resources") + f"{self.resource.uuid.hex}/"


class RequestChangesTest(ChangeRequestBase):
    def test_applicant_edits_exactly_the_unlocked_parts_and_submits(self):
        response = self.request_changes()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        response = self.update_details(project_summary="Shorter summary")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self.update_details(name="Another name")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("name", response.data)
        # The name sent as it is is no change.
        response = self.update_details(
            name=self.proposal.name, project_summary="Shortest"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.force_authenticate(self.applicant)
        response = self.client.patch(
            self.resource_url(), {"limits": {"cpu": 2}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        old_deadline = self.instance.deadline
        response = self.submit_changes()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [row["resolution"] for row in response.data],
            [ChangeRequestResolutions.SUBMITTED],
        )

        self.proposal.refresh_from_db()
        self.instance.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(self.proposal.project_summary, "Shortest")
        self.assertEqual(self.proposal.current_step_id, self.instance.pk)
        self.assertEqual(self.instance.status, ACTIVE)
        self.assertGreaterEqual(self.instance.deadline, old_deadline)
        # First come, first served reads when the proposal left draft.
        self.assertEqual(self.proposal.submitted_at, self.submitted_at)

        # Locked again.
        response = self.update_details(project_summary="Once more")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_changes_can_be_requested_again_and_the_rounds_are_kept(self):
        self.request_changes()
        self.submit_changes()
        response = self.request_changes(parts=["resources"])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.force_authenticate(self.applicant)
        response = self.client.get(self._url("change_requests"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["resolution"] for row in response.data],
            [ChangeRequestResolutions.SUBMITTED, ""],
        )
        self.assertEqual(response.data[1]["unlocked_parts"], ["resources"])
        # Who asked is not told to the applicant team.
        self.assertIsNone(response.data[0]["requested_by_name"])

    def test_only_one_request_is_open_at_a_time(self):
        self.request_changes()
        response = self.request_changes()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_parts_must_be_ones_the_proposal_has(self):
        response = self.request_changes(parts=["field:track"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.request_changes(parts=["compliance"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_step_that_does_not_allow_it_refuses(self):
        self.call_step.allows_change_requests = False
        self.call_step.save()
        response = self.request_changes()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_who_does_not_act_on_the_step_may_not_request_changes(self):
        offering_manager = structure_factories.UserFactory()
        self.fixture.offering.add_user(offering_manager, "OFFERING.MANAGER")
        for user in (
            self.applicant,
            self.fixture.reviewer_1,
            self.fixture.panel_member,
        ):
            response = self.request_changes(user=user)
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                user,
            )
        self.assertFalse(models.ProposalChangeRequest.objects.exists())

    def test_a_call_manager_on_the_applicant_team_may_not(self):
        self.proposal.add_user(self.call_manager, ProposalRole.MEMBER)
        response = self.request_changes()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_step_is_decided_only_once_the_request_is_closed(self):
        self.request_changes()
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self._url("complete_workflow_step"),
            {"step_uuid": self.instance.uuid.hex, "outcome": "eligible"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(
            self._url("reject_workflow_step"),
            {"step_uuid": self.instance.uuid.hex, "reason": "Incomplete"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        change_request = models.ProposalChangeRequest.objects.get()
        response = self.client.post(
            self._url("cancel_change_request"),
            {"change_request_uuid": change_request.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["resolution"], ChangeRequestResolutions.CANCELLED
        )
        response = self.client.post(
            self._url("complete_workflow_step"),
            {"step_uuid": self.instance.uuid.hex, "outcome": "eligible"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_applicant_may_not_cancel(self):
        self.request_changes()
        change_request = models.ProposalChangeRequest.objects.get()
        self.client.force_authenticate(self.applicant)
        response = self.client.post(
            self._url("cancel_change_request"),
            {"change_request_uuid": change_request.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_submitting_without_a_request_is_refused(self):
        response = self.submit_changes()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_an_outsider_may_not_submit_the_changes(self):
        self.request_changes()
        member = structure_factories.UserFactory()
        self.proposal.add_user(member, ProposalRole.MEMBER)
        response = self.submit_changes(user=member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_withdrawing_closes_the_request(self):
        self.request_changes()
        self.client.force_authenticate(self.applicant)
        response = self.client.post(self._url("withdraw"), {"reason": "No"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        change_request = models.ProposalChangeRequest.objects.get()
        self.assertEqual(change_request.resolution, ChangeRequestResolutions.WITHDRAWN)


class SubmitChangesChecksUnlockedPartsTest(ChangeRequestBase):
    """Submitting the changes checks only what the applicant could change: a
    gap elsewhere is the call team's to mend, and would otherwise hold the
    request open with nothing the applicant can do about it."""

    def setUp(self):
        super().setUp()
        checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.PROPOSAL_COMPLIANCE
        )
        checklist_factories.QuestionFactory(
            checklist=checklist,
            question_type=checklist_enums.QuestionTypes.BOOLEAN,
            required=True,
        )
        # Added to the call after the proposal was submitted: unanswered.
        self.call.compliance_checklist = checklist
        self.call.save()

    def can_submit(self):
        self.client.force_authenticate(self.applicant)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["can_submit"]

    def test_a_gap_in_a_locked_part_does_not_hold_the_changes(self):
        self.request_changes(parts=["field:project_summary"])
        self.assertEqual(self.can_submit(), {"can_submit": True, "error": None})

        self.update_details(project_summary="Names the codes it benchmarks")
        response = self.submit_changes()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_gap_in_an_unlocked_part_does(self):
        self.request_changes(parts=["compliance"])
        verdict = self.can_submit()
        self.assertFalse(verdict["can_submit"])
        self.assertIn("compliance checklist", verdict["error"])

        response = self.submit_changes()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("compliance checklist", str(response.data))


class ChangeRequestEditGatesTest(ChangeRequestBase):
    def test_documents_are_locked_in_review_without_a_request(self):
        document = models.ProposalDocumentation.objects.create(
            proposal=self.proposal,
            file=SimpleUploadedFile("form.pdf", b"%PDF-1.4\n%%EOF\n"),
        )
        self.client.force_authenticate(self.applicant)
        response = self.client.post(
            self._url("detach_documents"),
            {"documents": [document.uuid.hex]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(
            models.ProposalDocumentation.objects.filter(pk=document.pk).exists()
        )

        self.request_changes(parts=["documents"])
        response = self.client.post(
            self._url("detach_documents"),
            {"documents": [document.uuid.hex]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(
            models.ProposalDocumentation.objects.filter(pk=document.pk).exists()
        )

    def test_compliance_answers_are_locked_in_review_without_a_request(self):
        checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.PROPOSAL_COMPLIANCE
        )
        question = checklist_factories.QuestionFactory(
            checklist=checklist,
            question_type=checklist_enums.QuestionTypes.BOOLEAN,
        )
        self.call.compliance_checklist = checklist
        self.call.save()
        checklist_models.ChecklistCompletion.objects.get_or_create(
            scope_content_type=ContentType.objects.get_for_model(self.proposal),
            scope_object_id=self.proposal.id,
            checklist=checklist,
        )

        def answer():
            self.client.force_authenticate(self.applicant)
            return self.client.post(
                self._url("submit_answers"),
                [{"question_uuid": question.uuid.hex, "answer_data": True}],
                format="json",
            )

        self.assertEqual(answer().status_code, status.HTTP_409_CONFLICT)
        self.request_changes(parts=["compliance"])
        response = answer()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_resources_change_but_offerings_stay_once_assessed(self):
        factories.add_limit_component(
            self.fixture.requested_offering_accepted, "storage"
        )
        self.request_changes(parts=["resources"])
        self.client.force_authenticate(self.applicant)
        url = self.resource_url()
        response = self.client.patch(
            url, {"limits": {"storage": 2}, "attributes": {}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        models.StepContribution.objects.create(
            proposal_step=self.instance,
            requested_offering=self.fixture.requested_offering_accepted,
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_team_is_the_applicants_to_change_only_while_unlocked(self):
        member = structure_factories.UserFactory()
        role = ProposalRole.MEMBER

        def guard():
            handlers.guard_proposal_team_change(
                self.proposal, role, self.applicant, RoleChange.GRANT, member
            )

        with self.assertRaises(ValidationError):
            guard()
        self.request_changes(parts=["team"])
        self.proposal.refresh_from_db()
        guard()

    def test_the_unlocked_part_is_read_under_the_proposals_lock(self):
        """Submitting the changes, or the request lapsing, locks the
        proposal: an edit checked under the same lock cannot land after the
        request that allowed it has closed."""
        self.request_changes(parts=["documents"])
        self.proposal.refresh_from_db()
        with CaptureQueriesContext(connection) as queries:
            with transaction.atomic():
                change_requests.require_applicant_may_edit(
                    self.proposal, ChangeRequestParts.DOCUMENTS
                )
        self.assertTrue(
            any(
                'FROM "proposal_proposal"' in query["sql"]
                and "FOR UPDATE" in query["sql"]
                for query in queries.captured_queries
            )
        )

    def test_the_applicant_does_not_add_an_evaluator_to_the_team(self):
        reviewer = self.fixture.reviewer_1
        self.request_changes(parts=["team"])
        self.proposal.refresh_from_db()

        with self.assertRaises(ValidationError):
            handlers.guard_proposal_team_change(
                self.proposal,
                ProposalRole.MEMBER,
                self.applicant,
                RoleChange.GRANT,
                reviewer,
            )
        # The call's managers still may.
        handlers.guard_proposal_team_change(
            self.proposal,
            ProposalRole.MEMBER,
            self.fixture.call_manager,
            RoleChange.GRANT,
            reviewer,
        )


class ChangeRequestLapseTest(ChangeRequestBase):
    def test_step_does_not_lapse_while_waiting_on_the_applicant(self):
        self.request_changes()
        self.instance.deadline = timezone.now() - datetime.timedelta(hours=1)
        self.instance.save()
        tasks.mark_expired_workflow_steps()
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, ACTIVE)

    def test_a_lapsed_request_returns_the_step_to_its_actor(self):
        self.request_changes(
            due_at=(timezone.now() + datetime.timedelta(days=1)).isoformat()
        )
        models.ProposalChangeRequest.objects.update(
            due_at=timezone.now() - datetime.timedelta(minutes=1)
        )
        self.instance.deadline = timezone.now() - datetime.timedelta(hours=1)
        self.instance.save()
        with (
            self.captureOnCommitCallbacks(execute=True),
            run_now(tasks.notify_change_request),
        ):
            self.assertEqual(tasks.lapse_change_requests(), 1)
        change_request = models.ProposalChangeRequest.objects.get()
        self.assertEqual(change_request.resolution, ChangeRequestResolutions.LAPSED)
        self.instance.refresh_from_db()
        self.proposal.refresh_from_db()
        self.assertEqual(self.instance.status, ACTIVE)
        self.assertGreater(self.instance.deadline, timezone.now())
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertIn(self.call_manager.email, [m.to[0] for m in mail.outbox])
        # The applicant's fields are locked again.
        response = self.update_details(project_summary="Late")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_round_end_does_not_touch_a_proposal_sent_back(self):
        self.request_changes()
        self.fixture.round.cutoff_time = timezone.now() - datetime.timedelta(days=1)
        self.fixture.round.save()
        tasks.proposals_for_ended_rounds_should_be_cancelled()
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        response = self.submit_changes()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class ChangeRequestNotificationTest(ChangeRequestBase):
    def test_applicant_is_told_what_to_change(self):
        with run_now(tasks.notify_change_request):
            self.request_changes()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.applicant.email])
        self.assertIn("Attach the signed form", message.body)
        self.assertIn("project summary", message.body)

    def test_call_team_is_told_of_the_changes_submitted(self):
        self.request_changes()
        mail.outbox = []
        with run_now(tasks.notify_changes_submitted):
            self.submit_changes()
        recipients = {address for message in mail.outbox for address in message.to}
        self.assertIn(self.call_manager.email, recipients)
        self.assertNotIn(self.applicant.email, recipients)
        [message] = [m for m in mail.outbox if m.to == [self.call_manager.email]]
        self.assertIn("project summary", message.body)

    def test_rules_can_tell_others_of_each_round(self):
        models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.call_step,
            trigger=NotificationRuleTriggers.CHANGES_REQUESTED,
            recipient=NotificationRuleRecipients.CALL_MANAGERS,
        )
        with run_now(tasks.notify_workflow_step_event):
            self.request_changes()
            self.submit_changes()
            self.request_changes()
        rule_mails = [
            message
            for message in mail.outbox
            if message.to == [self.call_manager.email]
            and "Changes requested" in message.subject
        ]
        self.assertEqual(len(rule_mails), 2)

    def test_feed_names_no_requester_for_the_applicant(self):
        self.request_changes()
        event = Event.objects.get(event_type=EventType.PROPOSAL_CHANGES_REQUESTED)
        self.assertNotIn("user_uuid", event.context)


class OpenChangeRequestFieldTest(ChangeRequestBase):
    def _read(self, user):
        self.client.force_authenticate(user)
        return self.client.get(factories.ProposalFactory.get_url(self.proposal))

    def test_applicant_and_call_team_read_it_others_do_not(self):
        self.assertEqual(self._read(self.applicant).data["open_change_requests"], [])
        self.request_changes()
        [data] = self._read(self.applicant).data["open_change_requests"]
        self.assertEqual(
            data["message"], "Attach the signed form and shorten the summary."
        )
        self.assertIsNone(data["requested_by_name"])
        [data] = self._read(self.call_manager).data["open_change_requests"]
        self.assertIsNotNone(data["requested_by_name"])
        outsider = structure_factories.UserFactory()
        response = self._read(outsider)
        if response.status_code == status.HTTP_200_OK:
            self.assertEqual(response.data["open_change_requests"], [])

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(
            factories.ProposalFactory.get_list_url(), {"changes_requested": True}
        )
        self.assertEqual(
            [row["uuid"] for row in response.data], [self.proposal.uuid.hex]
        )


class AllowsChangeRequestsConfigTest(test.APITestCase):
    def test_not_on_the_allocation_decision(self):
        fixture = fixtures.ProposalFixture()
        step = models.CallWorkflowStep.objects.get(
            call=fixture.call, is_allocation_decision=True
        )
        step.allows_change_requests = True
        with self.assertRaises(DjangoValidationError):
            step.clean()


class ReopenContributionTest(PerOfferingAssessmentBase):
    def setUp(self):
        super().setUp()
        self.proposal.refresh_from_db()

    def _reopen(self, user, contribution, message="Recheck the GPU hours."):
        self.client.force_authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                self._url("reopen_contribution"),
                {"contribution_uuid": contribution.uuid.hex, "message": message},
                format="json",
            )

    def _fill(self, user, slot, outcome=ContributionOutcomes.ACCEPTED):
        self._answer(user, slot)
        response = self._submit(user, slot, outcome)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def test_reopened_while_the_step_is_active_and_resubmitted(self):
        slot_a = self._slot(self.requested_a)
        self._fill(self.manager_a, slot_a)
        response = self._reopen(self.call_manager, slot_a)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        slot_a.refresh_from_db()
        self.assertEqual(slot_a.status, StepContributionStatuses.PENDING)
        self.assertEqual(slot_a.outcome, "")

        self._fill(self.manager_a, slot_a, ContributionOutcomes.REJECTED)
        change_request = models.ProposalChangeRequest.objects.get()
        self.assertEqual(change_request.resolution, ChangeRequestResolutions.SUBMITTED)
        response = self._fill(self.manager_b, self._slot(self.requested_b))
        self.assertTrue(response.data["step_completed"])

    def test_reopened_while_parked_reactivates_the_step(self):
        self.call_step.transition_mode = TransitionModes.MANUAL
        self.call_step.save()
        slot_a = self._slot(self.requested_a)
        self._fill(self.manager_a, slot_a)
        self._fill(self.manager_b, self._slot(self.requested_b))
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowStepInstanceStatuses.COMPLETED)

        response = self._reopen(self.call_manager, slot_a)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, ACTIVE)
        self.assertIsNone(self.instance.outcome)

        response = self._fill(self.manager_a, slot_a)
        self.assertTrue(response.data["step_completed"])
        self.instance.refresh_from_db()
        self.proposal.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(self.proposal.current_step_id, self.instance.pk)

    def test_refused_once_the_next_step_started(self):
        slot_a = self._slot(self.requested_a)
        self._fill(self.manager_a, slot_a)
        self._fill(self.manager_b, self._slot(self.requested_b))
        self.proposal.refresh_from_db()
        self.assertNotEqual(self.proposal.current_step_id, self.instance.pk)
        response = self._reopen(self.call_manager, slot_a)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_the_call_team_reopens(self):
        slot_a = self._slot(self.requested_a)
        self._fill(self.manager_a, slot_a)
        for user in (self.manager_a, self.manager_b, self.proposal.created_by):
            response = self._reopen(user, slot_a)
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )
        slot_a.refresh_from_db()
        self.assertEqual(slot_a.status, StepContributionStatuses.SUBMITTED)

    def test_contributor_is_told_and_the_applicant_never_reads_it(self):
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        slot_a = self._slot(self.requested_a)
        self._fill(self.manager_a, slot_a)
        mail.outbox = []
        with run_now(tasks.notify_change_request):
            self._reopen(self.call_manager, slot_a)
        recipients = {address for message in mail.outbox for address in message.to}
        self.assertEqual(recipients, {self.manager_a.email})
        self.assertIn("Recheck the GPU hours.", mail.outbox[0].body)

        for user, expected in (
            (self.proposal.created_by, 0),
            (self.manager_b, 0),
            (self.manager_a, 1),
            (self.call_manager, 1),
        ):
            self.client.force_authenticate(user)
            response = self.client.get(self._url("change_requests"))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data), expected, user)


class SubmitChangesTaskTest(ChangeRequestBase):
    def _needs(self, user):
        return [
            (task.needed, task.proposal)
            for task in my_tasks.tasks_of(user)
            if task.needed == my_tasks.Needs.SUBMIT_CHANGES
        ]

    def test_the_applicant_managers_are_asked_until_the_changes_are_in(self):
        self.assertEqual(self._needs(self.applicant), [])
        due_at = timezone.now() + datetime.timedelta(days=5)
        self.request_changes(due_at=due_at.isoformat())
        tasks = [
            task
            for task in my_tasks.tasks_of(self.applicant)
            if task.needed == my_tasks.Needs.SUBMIT_CHANGES
        ]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].proposal, self.proposal)
        self.assertEqual(tasks[0].target, my_tasks.Targets.PROPOSAL)
        self.assertEqual(tasks[0].due_at, due_at)

        self.submit_changes()
        self.assertEqual(self._needs(self.applicant), [])

    def test_a_cancelled_request_leaves_the_list(self):
        self.request_changes()
        self.client.force_authenticate(self.call_manager)
        self.client.post(
            self._url("cancel_change_request"),
            {
                "change_request_uuid": models.ProposalChangeRequest.objects.get().uuid.hex
            },
            format="json",
        )
        self.assertEqual(self._needs(self.applicant), [])

    def test_nobody_else_is_asked(self):
        member = structure_factories.UserFactory()
        self.proposal.add_user(member, ProposalRole.MEMBER)
        other = factories.ProposalFactory(round=self.fixture.round)
        other_manager = fixtures.author_as_manager(other)
        self.request_changes()
        for user in (
            self.call_manager,
            self.fixture.reviewer_1,
            member,
            other_manager,
        ):
            self.assertEqual(self._needs(user), [], user)

    def test_listed_through_the_api(self):
        self.request_changes()
        self.client.force_authenticate(self.applicant)
        response = self.client.get("/api/proposal-my-tasks/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        needs = [row["needed"] for row in response.data]
        self.assertIn(my_tasks.Needs.SUBMIT_CHANGES, needs)


class ChangeRequestInParallelStageTest(ParallelStageBase):
    """Changes asked of the applicant at the steps of a stage running at
    once: each step may ask, the applicant answers them together."""

    def setUp(self):
        super().setUp()
        ProposalRole.MANAGER.add_permission(PermissionEnum.UPDATE_PROPOSAL)
        ProposalRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL)
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        for step in (self.panel, self.technical):
            step.allows_change_requests = True
            step.duration_in_days = 14
            step.save(update_fields=["allows_change_requests", "duration_in_days"])
        self.applicant = self.proposal.created_by
        self.proposal.name = "Exascale climate runs"
        self.proposal.save(update_fields=["name"])

    def _request_changes(self, key, parts=None, message=None, due_at=None):
        self.client.force_authenticate(self.call_manager)
        payload = {
            "step_uuid": self._instance(key).uuid.hex,
            "message": message or "Attach the signed form.",
            "parts": parts or ["documents"],
        }
        if due_at is not None:
            payload["due_at"] = due_at.isoformat()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                self._url("request_changes"), payload, format="json"
            )

    def _both_ask(self, **due):
        response = self._request_changes(
            "panel_review",
            ["field:project_summary", "documents"],
            "Shorten the summary.",
            due.get("panel"),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self._request_changes(
            "technical_assessment",
            ["field:description", "documents"],
            "Describe the GPU workload.",
            due.get("technical"),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def _update_details(self, **data):
        self.client.force_authenticate(self.applicant)
        return self.client.post(
            self._url("update_project_details"),
            {"name": self.proposal.name, **data},
            format="json",
        )

    def _submit_changes(self):
        self.client.force_authenticate(self.applicant)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self._url("submit_changes"))

    def _cancel(self, key):
        change_request = models.ProposalChangeRequest.objects.get(
            step_instance=self._instance(key), closed_at__isnull=True
        )
        self.client.force_authenticate(self.call_manager)
        return self.client.post(
            self._url("cancel_change_request"),
            {"change_request_uuid": change_request.uuid.hex},
            format="json",
        )

    def _read(self, user):
        self.client.force_authenticate(user)
        return self.client.get(factories.ProposalFactory.get_url(self.proposal))

    def test_the_applicant_edits_every_part_any_open_request_unlocks(self):
        self._both_ask()
        response = self._update_details(project_summary="Shorter")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self._update_details(description="GPU workload")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self._update_details(name="Another name")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_the_applicant_reads_every_open_request_but_not_who_asked(self):
        self._both_ask()
        rows = self._read(self.applicant).data["open_change_requests"]
        self.assertEqual(
            [(row["step_key"], row["message"]) for row in rows],
            [
                ("panel_review", "Shorten the summary."),
                ("technical_assessment", "Describe the GPU workload."),
            ],
        )
        self.assertEqual(
            [row["unlocked_parts"] for row in rows],
            [
                ["field:project_summary", "documents"],
                ["field:description", "documents"],
            ],
        )
        self.assertEqual([row["requested_by_name"] for row in rows], [None, None])
        rows = self._read(self.call_manager).data["open_change_requests"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["requested_by_name"] for row in rows))
        # Nor does the feed tell the applicant who asked.
        for event in Event.objects.filter(
            event_type=EventType.PROPOSAL_CHANGES_REQUESTED
        ):
            self.assertNotIn("user_uuid", event.context)

    def test_a_cancelled_request_locks_only_the_parts_no_other_unlocks(self):
        self._both_ask()
        response = self._cancel("panel_review")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self._update_details(project_summary="Shorter")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        response = self._update_details(description="GPU workload")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertTrue(
            change_requests.applicant_may_edit(
                self.proposal, ChangeRequestParts.DOCUMENTS
            )
        )
        self.assertEqual(
            [
                row["step_key"]
                for row in self._read(self.applicant).data["open_change_requests"]
            ],
            ["technical_assessment"],
        )
        self.assertEqual(self._cancel("technical_assessment").status_code, 200)
        self.assertFalse(
            change_requests.applicant_may_edit(
                self.proposal, ChangeRequestParts.DOCUMENTS
            )
        )

    def test_one_submission_answers_every_request(self):
        self._both_ask()
        submitted_at = self.proposal.submitted_at
        soon = timezone.now() + datetime.timedelta(hours=1)
        self.proposal.workflow_step_instances.filter(status=ACTIVE).update(
            deadline=soon
        )
        mail.outbox = []
        with run_now(tasks.notify_changes_submitted):
            response = self._submit_changes()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [(row["step_key"], row["resolution"]) for row in response.data],
            [
                ("panel_review", ChangeRequestResolutions.SUBMITTED),
                ("technical_assessment", ChangeRequestResolutions.SUBMITTED),
            ],
        )
        self.assertFalse(
            models.ProposalChangeRequest.objects.filter(closed_at__isnull=True).exists()
        )
        for key in ("panel_review", "technical_assessment"):
            instance = self._instance(key)
            self.assertEqual(instance.status, ACTIVE)
            self.assertGreater(
                instance.deadline, timezone.now() + datetime.timedelta(days=13)
            )
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(self.proposal.submitted_at, submitted_at)
        response = self._update_details(project_summary="Once more")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        # One event names both requests; each step's actors are told.
        event = Event.objects.get(event_type=EventType.PROPOSAL_CHANGES_SUBMITTED)
        self.assertEqual(
            [row["step_name"] for row in event.context["change_requests"]],
            ["Panel review", "Technical assessment"],
        )

        # Each recipient one mail, naming every request that concerns them:
        # the call manager both steps, an offering manager their own.
        def mails_to(user):
            return [m for m in mail.outbox if user.email in m.to]

        [message] = mails_to(self.call_manager)
        self.assertIn('"Panel review"', message.body)
        self.assertIn('"Technical assessment"', message.body)
        self.assertIn("project summary", message.body)
        self.assertIn("description", message.body)
        for manager in (self.manager_a, self.manager_b):
            [message] = mails_to(manager)
            self.assertIn('"Technical assessment"', message.body)
            self.assertNotIn("Panel review", message.body)
        self.assertEqual(mails_to(self.applicant), [])

        response = self._submit_changes()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_only_a_step_with_its_own_request_waits_for_it(self):
        self._both_ask()
        response = self._complete(
            self.call_manager, "panel_review", WorkflowStepOutcomes.APPROVED
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self._cancel("panel_review")
        response = self._complete(
            self.call_manager, "panel_review", WorkflowStepOutcomes.APPROVED
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self._instance("panel_review").status,
            WorkflowStepInstanceStatuses.COMPLETED,
        )
        # The technical assessment still waits on its own request.
        self.assertEqual(self._instance("technical_assessment").status, ACTIVE)
        self.assertEqual(
            [
                row["step_key"]
                for row in self._read(self.applicant).data["open_change_requests"]
            ],
            ["technical_assessment"],
        )

    def test_one_task_due_when_the_earliest_request_is(self):
        later = timezone.now() + datetime.timedelta(days=10)
        sooner = timezone.now() + datetime.timedelta(days=3)
        self._both_ask(panel=later, technical=sooner)
        found = [
            task
            for task in my_tasks.tasks_of(self.applicant)
            if task.needed == my_tasks.Needs.SUBMIT_CHANGES
        ]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].due_at, sooner)
        self.assertEqual(found[0].instance.step, "technical_assessment")
        self._submit_changes()
        self.assertFalse(
            [
                task
                for task in my_tasks.tasks_of(self.applicant)
                if task.needed == my_tasks.Needs.SUBMIT_CHANGES
            ]
        )

    def _reopen(self, contribution):
        self.client.force_authenticate(self.call_manager)
        return self.client.post(
            self._url("reopen_contribution"),
            {"contribution_uuid": contribution.uuid.hex, "message": "Recheck it."},
            format="json",
        )

    def test_the_request_holds_only_the_step_that_made_it(self):
        response = self._request_changes("panel_review")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        response = self._complete(
            self.call_manager, "panel_review", WorkflowStepOutcomes.APPROVED
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self._reject(self.call_manager, "panel_review")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        # The other step is decided as usual.
        self._assess_all()
        self.assertEqual(
            self._instance("technical_assessment").status,
            WorkflowStepInstanceStatuses.COMPLETED,
        )
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.current_step.step, "panel_review")
        self.assertEqual(self._instance("allocation_decision").status, "pending")

    def test_only_the_step_waiting_on_the_applicant_does_not_lapse(self):
        self._request_changes("panel_review")
        past = timezone.now() - datetime.timedelta(minutes=1)
        self.proposal.workflow_step_instances.filter(status=ACTIVE).update(
            deadline=past
        )
        tasks.mark_expired_workflow_steps()
        self.assertEqual(
            self._instance("technical_assessment").status,
            WorkflowStepInstanceStatuses.EXPIRED,
        )
        self.assertEqual(self._instance("panel_review").status, ACTIVE)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(self.proposal.current_step.step, "panel_review")

    def test_a_rejection_beside_it_calls_the_request_off(self):
        self._request_changes("panel_review")
        response = self._reject(self.call_manager, "technical_assessment")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._instance("panel_review").status, "withdrawn")
        change_request = models.ProposalChangeRequest.objects.get()
        self.assertEqual(change_request.resolution, ChangeRequestResolutions.CANCELLED)


class ReopenInParkedStageTest(ParallelStageBase):
    """A stage parked on its manual step, whose other step's work is reopened:
    the stage moves on once, when the call managers advance it again."""

    panel_mode = TransitionModes.MANUAL

    def _reopen(self, contribution):
        self.client.force_authenticate(self.call_manager)
        return self.client.post(
            self._url("reopen_contribution"),
            {"contribution_uuid": contribution.uuid.hex, "message": "Recheck it."},
            format="json",
        )

    def test_reactivating_a_completed_member_does_not_start_the_next_stage(self):
        self._complete(self.panel_member, "panel_review", WorkflowStepOutcomes.APPROVED)
        self._assess_all()
        self.proposal.refresh_from_db()
        self.assertTrue(workflow_service.is_awaiting_manual_advance(self.proposal))
        technical = self._instance("technical_assessment")
        slot_a = technical.contributions.get(requested_offering=self.requested_a)

        response = self._reopen(slot_a)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._instance("technical_assessment").status, ACTIVE)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.current_step.step, "technical_assessment")
        self.assertFalse(workflow_service.is_awaiting_manual_advance(self.proposal))
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(self._url("advance_workflow_step"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self._instance("allocation_decision").status, "pending")

        with mock.patch(DISPATCH) as dispatch:
            self._assess(
                "technical_assessment",
                self.requested_a,
                self.manager_a,
                ContributionOutcomes.ACCEPTED,
            )
            self.proposal.refresh_from_db()
            self.assertTrue(workflow_service.is_awaiting_manual_advance(self.proposal))
            self.assertEqual(self.proposal.current_step.step, "panel_review")
            self.assertEqual(self._instance("allocation_decision").status, "pending")
            self.client.force_authenticate(self.call_manager)
            response = self.client.post(self._url("advance_workflow_step"))
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        started = [
            call.args[0].step
            for call in dispatch.call_args_list
            if call.args[1] == NotificationRuleTriggers.STEP_STARTED
        ]
        self.assertEqual(started, ["allocation_decision"])

    def test_work_of_a_stage_the_proposal_left_is_not_reopened(self):
        self._complete(self.panel_member, "panel_review", WorkflowStepOutcomes.APPROVED)
        self._assess_all()
        self.client.force_authenticate(self.call_manager)
        self.client.post(self._url("advance_workflow_step"))
        slot_a = self._instance("technical_assessment").contributions.get(
            requested_offering=self.requested_a
        )
        response = self._reopen(slot_a)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
