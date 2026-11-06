"""Steps run once per round: opening them, round reports, due dates per
round, and moving a round's proposals on together."""

from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist.enums import ChecklistTypes, QuestionTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    contributions,
    models,
    my_tasks,
    proposal_roles,
    round_reports,
    round_steps,
    tasks,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    CallStates,
    COIStatuses,
    ContributorSources,
    EvaluationStart,
    NotificationRuleTriggers,
    OutcomeSets,
    ProposalRoleEligibility,
    ProposalStates,
    ResponsibleRoles,
    RoundLifecycleStates,
    StepKinds,
    TransitionModes,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories

NOTES_URL = "http://testserver" + reverse("round-meeting-note-list")
RANKINGS_URL = "http://testserver" + reverse("proposal-ranking-list")
AUTHORS_MAIL = "waldur_mastermind.proposal.tasks.notify_round_report_authors.delay"
MANAGERS_MAIL = (
    "waldur_mastermind.proposal.tasks.notify_call_managers_about_round_step.delay"
)
STEP_EVENT = "waldur_mastermind.proposal.tasks.notify_workflow_step_event.delay"


def note_url(note, action=None):
    url = "http://testserver" + reverse(
        "round-meeting-note-detail", kwargs={"uuid": note.uuid.hex}
    )
    return url + f"{action}/" if action else url


class RoundStepTestBase(test.APITestCase):
    """A call evaluating at the cut-off: a check the call managers move on
    by hand, a domain panel meeting run once per round whose reports the
    proposals' domain chairs write, and the allocation decision. Three
    proposals: chair A holds the first two, chair B the third."""

    def setUp(self):
        for permission in (
            PermissionEnum.LIST_CALLS,
            PermissionEnum.LIST_PROPOSALS,
            PermissionEnum.LIST_ROUNDS,
            PermissionEnum.UPDATE_CALL,
            PermissionEnum.CLOSE_ROUNDS,
        ):
            CallRole.MANAGER.add_permission(permission)
        CallRole.PANEL_MEMBER.add_permission(PermissionEnum.LIST_CALLS)
        self.call = factories.CallFactory(
            state=CallStates.ACTIVE, evaluation_start=EvaluationStart.AT_CUTOFF
        )
        self.call.workflow_steps.all().delete()
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)
        self.chair_a = self.panel_member()
        self.chair_b = self.panel_member()
        self.outsider = self.panel_member()
        self.role = models.CallProposalRole.objects.create(
            call=self.call,
            key="domain_panel_chair",
            name="Domain Panel Chair",
            eligible_from=ProposalRoleEligibility.CALL_PANEL_MEMBER,
            min_holders=1,
            max_holders=1,
        )
        self.checklist = checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.WORKFLOW_STEP
        )
        self.question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            required=True,
            question_type=QuestionTypes.TEXT_AREA,
        )
        self.check = self.add_step(
            "check",
            StepKinds.DECISION,
            0,
            outcome_set=OutcomeSets.APPROVAL,
            completer=ResponsibleRoles.CALL_MANAGER,
            transition_mode=TransitionModes.MANUAL,
        )
        self.meeting = self.add_step(
            "meeting",
            StepKinds.ASSESSMENT,
            1,
            outcome_set=OutcomeSets.DONE,
            round_scoped=True,
            contributor_source=f"{ContributorSources.PROPOSAL_ROLE}:{self.role.pk}",
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.role,
            checklist=self.checklist,
            duration_in_days=10,
        )
        self.decision = self.add_step(
            "allocation_decision",
            StepKinds.DECISION,
            2,
            outcome_set=OutcomeSets.APPROVAL,
            completer=ResponsibleRoles.CALL_MANAGER,
            is_allocation_decision=True,
        )
        now = timezone.now()
        self.round = factories.RoundFactory(
            call=self.call,
            start_time=now - timedelta(days=30),
            cutoff_time=now - timedelta(minutes=1),
        )
        self.round.lifecycle_state = RoundLifecycleStates.EVALUATING
        self.round.save(update_fields=["lifecycle_state"])
        self.first = self.make_proposal(self.chair_a)
        self.second = self.make_proposal(self.chair_a)
        self.third = self.make_proposal(self.chair_b)

    def panel_member(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.PANEL_MEMBER)
        return user

    def add_step(self, key, kind, order, **values):
        return models.CallWorkflowStep.objects.create(
            call=self.call, key=key, kind=kind, name=key.title(), order=order, **values
        )

    def make_proposal(self, chair):
        proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        models.ProposalRoleAssignment.objects.create(
            proposal=proposal, role=self.role, user=chair
        )
        with transaction.atomic():
            workflow_service.create_step_instances(proposal)
            workflow_service.activate_first_step(proposal)
        proposal.refresh_from_db()
        return proposal

    def pass_check(self, proposal):
        proposal.refresh_from_db()
        with transaction.atomic():
            workflow_service.complete_step(
                proposal,
                proposal.current_step,
                "approved",
                "",
                completed_by=self.manager,
            )
        proposal.refresh_from_db()

    def instance(self, proposal, call_step=None):
        return proposal.workflow_step_instances.get(call_step=call_step or self.meeting)

    def open(self, user=None, **data):
        self.client.force_authenticate(user or self.manager)
        url = factories.RoundFactory.get_url(
            self.call, self.round, "open_workflow_step"
        )
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                url, {"step": self.meeting.key, **data}, format="json"
            )

    def open_all(self):
        for proposal in (self.first, self.second, self.third):
            self.pass_check(proposal)
        with mock.patch(AUTHORS_MAIL), mock.patch(MANAGERS_MAIL):
            response = self.open()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def report_of(self, user):
        return models.RoundMeetingNote.objects.get(
            round=self.round, call_step=self.meeting, author=user
        )

    def answer(self, note, user=None):
        self.client.force_authenticate(user or note.author)
        return self.client.post(
            note_url(note, "submit_answers"),
            [{"question_uuid": self.question.uuid.hex, "answer_data": "Discussed."}],
            format="json",
        )

    def submit(self, note, user=None):
        self.client.force_authenticate(user or note.author)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(note_url(note, "submit"))


class RoundStepConfigTest(RoundStepTestBase):
    def test_allocation_decision_cannot_run_once_per_round(self):
        self.decision.round_scoped = True
        with self.assertRaises(DjangoValidationError):
            self.decision.clean()

    def test_a_step_after_the_award_cannot_run_once_per_round(self):
        self.meeting.phase = WorkflowPhases.CLOSING
        with self.assertRaises(DjangoValidationError) as raised:
            self.meeting.clean()
        self.assertIn("round_scoped", raised.exception.message_dict)

    def test_step_counted_per_contribution_cannot_run_once_per_round(self):
        self.meeting.min_contributions = 1
        with self.assertRaises(DjangoValidationError):
            self.meeting.clean()

    def test_round_step_needs_evaluation_at_the_cut_off(self):
        self.call.evaluation_start = EvaluationStart.ON_SUBMISSION
        self.meeting.call = self.call
        with self.assertRaises(DjangoValidationError) as raised:
            self.meeting.clean()
        self.assertIn("round_scoped", raised.exception.message_dict)

    def test_call_with_a_round_step_keeps_evaluating_at_the_cut_off(self):
        models.Proposal.objects.filter(round=self.round).update(
            state=ProposalStates.DRAFT
        )
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            factories.CallFactory.get_protected_url(self.call),
            {"evaluation_start": EvaluationStart.ON_SUBMISSION},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_step_serializer_refuses_a_round_step_with_a_minimum(self):
        self.client.force_authenticate(self.manager)
        url = (
            factories.CallFactory.get_protected_url(self.call, action="workflow_steps")
            + self.meeting.uuid.hex
            + "/"
        )
        response = self.client.patch(url, {"max_contributions": 2}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("round_scoped", response.data)


class RoundStepEngineTest(RoundStepTestBase):
    def test_proposal_reaching_a_round_step_gets_no_work_and_no_mail(self):
        rule = models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.meeting,
            trigger=NotificationRuleTriggers.STEP_STARTED,
            recipient="call_managers",
        )
        self.assertTrue(rule.is_enabled)
        with mock.patch(STEP_EVENT) as step_event:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    self.first.refresh_from_db()
                    workflow_service.advance_step(self.first_parked())
        step_event.assert_not_called()
        instance = self.instance(self.first)
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertFalse(instance.contributions.exists())
        # Not opened yet: its duration does not run.
        self.assertIsNone(instance.deadline)
        self.assertIn("open this step", contributions.why_step_waits(instance))

    def first_parked(self):
        self.pass_check(self.first)
        return self.first

    def test_round_step_is_not_completed_on_one_proposal(self):
        self.open_all()
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.ProposalFactory.get_url(
                self.first, action="complete_workflow_step"
            ),
            {"step_uuid": self.instance(self.first).uuid.hex, "outcome": "done"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            self.instance(self.first).status, WorkflowStepInstanceStatuses.ACTIVE
        )

    def test_call_manager_can_still_reject_a_proposal_at_it(self):
        self.open_all()
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.ProposalFactory.get_url(
                self.first, action="reject_workflow_step"
            ),
            {"step_uuid": self.instance(self.first).uuid.hex, "reason": "Out of scope"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.first.refresh_from_db()
        self.assertEqual(self.first.state, ProposalStates.REJECTED)

    def test_a_lapsed_round_step_does_not_expire_proposals_one_by_one(self):
        self.open_all()
        instance = self.instance(self.first)
        instance.deadline = timezone.now() - timedelta(days=1)
        instance.save(update_fields=["deadline"])
        tasks.mark_expired_workflow_steps()
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.ACTIVE)


class OpenRoundStepTest(RoundStepTestBase):
    def test_opening_is_refused_while_proposals_lag(self):
        self.pass_check(self.first)
        response = self.open()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["lagging_count"], 2)
        self.assertFalse(models.RoundStep.objects.filter(opened_at__isnull=False))

    def test_forcing_needs_a_reason(self):
        response = self.open(force=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reason", response.data)

    def test_forced_opening_records_why_and_stragglers_join(self):
        self.pass_check(self.first)
        with mock.patch(AUTHORS_MAIL) as authors_mail, mock.patch(MANAGERS_MAIL):
            response = self.open(force=True, reason="Panel meets on Monday")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        round_step = models.RoundStep.objects.get(round=self.round)
        self.assertEqual(round_step.opened_reason, "Panel meets on Monday")
        self.assertEqual(
            list(self.report_of(self.chair_a).proposals.all()), [self.first]
        )
        authors_mail.assert_called_once()
        # The third proposal arrives later and joins its chair's new report,
        # who is asked for it then.
        with mock.patch(AUTHORS_MAIL) as authors_mail:
            with self.captureOnCommitCallbacks(execute=True):
                self.pass_check(self.third)
                with transaction.atomic():
                    workflow_service.advance_step(self.third, acting_user=self.manager)
        self.assertEqual(
            list(self.report_of(self.chair_b).proposals.all()), [self.third]
        )
        authors_mail.assert_called_once_with([self.report_of(self.chair_b).uuid.hex])

    def test_opening_moves_proposals_in_and_asks_each_chair_once(self):
        for proposal in (self.first, self.second, self.third):
            self.pass_check(proposal)
        due = timezone.now() + timedelta(days=5)
        with (
            mock.patch(AUTHORS_MAIL) as authors_mail,
            mock.patch(MANAGERS_MAIL) as managers_mail,
        ):
            response = self.open(due_at=due.isoformat())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["advanced"]), 3)
        self.assertEqual(response.data["reports_created"], 2)
        reports = models.RoundMeetingNote.objects.filter(call_step=self.meeting)
        self.assertEqual(reports.count(), 2)
        self.assertCountEqual(
            self.report_of(self.chair_a).proposals.all(), [self.first, self.second]
        )
        self.assertCountEqual(
            self.report_of(self.chair_b).proposals.all(), [self.third]
        )
        authors_mail.assert_called_once()
        self.assertCountEqual(
            authors_mail.call_args.args[0], [r.uuid.hex for r in reports]
        )
        managers_mail.assert_called_once()
        for proposal in (self.first, self.second, self.third):
            instance = self.instance(proposal)
            self.assertEqual(instance.status, WorkflowStepInstanceStatuses.ACTIVE)
            self.assertEqual(instance.deadline, due)

    def test_opening_again_changes_nothing(self):
        self.open_all()
        before = list(
            models.RoundMeetingNote.objects.order_by("pk").values_list("pk", "modified")
        )
        with (
            mock.patch(AUTHORS_MAIL) as authors_mail,
            mock.patch(MANAGERS_MAIL) as managers_mail,
        ):
            response = self.open()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["advanced"], [])
        self.assertEqual(response.data["reports_created"], 0)
        authors_mail.assert_not_called()
        managers_mail.assert_not_called()
        after = list(
            models.RoundMeetingNote.objects.order_by("pk").values_list("pk", "modified")
        )
        self.assertEqual(before, after)

    def test_only_call_managers_open_a_round_step(self):
        for proposal in (self.first, self.second, self.third):
            self.pass_check(proposal)
        response = self.open(user=self.chair_a)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_without_a_duration_or_due_date_the_opening_starts_the_clock(self):
        self.open_all()
        round_step = models.RoundStep.objects.get(round=self.round)
        self.assertEqual(
            self.instance(self.first).deadline,
            round_step.opened_at + timedelta(days=10),
        )


class RoundReportTest(RoundStepTestBase):
    def setUp(self):
        super().setUp()
        self.open_all()
        self.report_a = self.report_of(self.chair_a)
        self.report_b = self.report_of(self.chair_b)

    def test_author_edits_their_report_and_nobody_else_does(self):
        self.client.force_authenticate(self.chair_a)
        response = self.client.patch(
            note_url(self.report_a), {"body": "Notes"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.client.force_authenticate(self.chair_b)
        response = self.client.patch(
            note_url(self.report_a), {"body": "Mine now"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            note_url(self.report_a), {"body": "Edited"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_report_cannot_be_created_through_the_api(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            NOTES_URL,
            {
                "round": self.round.uuid.hex,
                "title": "Free",
                "call_step": self.meeting.uuid.hex,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        note = models.RoundMeetingNote.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(note.call_step)

    def test_submit_needs_the_form_answered(self):
        response = self.submit(self.report_a)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.answer(self.report_a).status_code, status.HTTP_200_OK)
        response = self.submit(self.report_a)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["completed"]), 2)

    def test_only_the_author_answers_and_submits(self):
        response = self.answer(self.report_a, user=self.manager)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.answer(self.report_a)
        response = self.submit(self.report_a, user=self.manager)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_author_who_lost_their_role_no_longer_writes_or_submits(self):
        self.answer(self.report_a)
        with transaction.atomic():
            for assignment in models.ProposalRoleAssignment.objects.filter(
                user=self.chair_a, revoked_at__isnull=True
            ):
                proposal_roles.revoke(assignment, revoked_by=self.manager)

        self.assertEqual(
            self.answer(self.report_a).status_code, status.HTTP_403_FORBIDDEN
        )
        self.client.force_authenticate(self.chair_a)
        response = self.client.patch(
            note_url(self.report_a), {"body": "Still mine"}, format="json"
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        response = self.submit(self.report_a)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.report_a.refresh_from_db()
        self.assertIsNone(self.report_a.submitted_at)

    def test_step_completes_for_the_proposals_whose_reports_are_all_in(self):
        self.answer(self.report_a)
        with self.captureOnCommitCallbacks(execute=True):
            self.submit(self.report_a)
        for proposal in (self.first, self.second):
            instance = self.instance(proposal)
            self.assertEqual(instance.status, WorkflowStepInstanceStatuses.COMPLETED)
            self.assertEqual(instance.outcome, "done")
            self.assertEqual(instance.completed_by, self.chair_a)
            proposal.refresh_from_db()
            self.assertEqual(proposal.workflow_step, "allocation_decision")
        self.assertEqual(
            self.instance(self.third).status, WorkflowStepInstanceStatuses.ACTIVE
        )
        self.assertEqual(
            contributions.why_step_waits(self.instance(self.third)),
            "Waiting for 1 round report.",
        )

    def test_proposal_with_two_authors_waits_for_both(self):
        co_chair = self.panel_member()
        self.role.max_holders = 2
        self.role.save(update_fields=["max_holders"])
        models.ProposalRoleAssignment.objects.create(
            proposal=self.first, role=self.role, user=co_chair
        )
        round_step = models.RoundStep.objects.get(round=self.round)
        with mock.patch(AUTHORS_MAIL), self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                round_reports.sync_reports(round_step)
        self.answer(self.report_a)
        self.submit(self.report_a)
        self.assertEqual(
            self.instance(self.first).status, WorkflowStepInstanceStatuses.ACTIVE
        )
        self.assertEqual(
            self.instance(self.second).status, WorkflowStepInstanceStatuses.COMPLETED
        )
        co_report = self.report_of(co_chair)
        self.answer(co_report)
        response = self.submit(co_report)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.instance(self.first).status, WorkflowStepInstanceStatuses.COMPLETED
        )

    def test_straggler_waits_for_a_reopened_report(self):
        self.answer(self.report_b)
        self.submit(self.report_b)
        fourth = self.make_proposal(self.chair_b)
        self.pass_check(fourth)
        with mock.patch(AUTHORS_MAIL), self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                workflow_service.advance_step(fourth, acting_user=self.manager)
        reason = contributions.why_step_waits(self.instance(fourth))
        self.assertIn("reopen it", reason)
        self.client.force_authenticate(self.chair_b)
        response = self.client.post(note_url(self.report_b, "reopen"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(self.manager)
        response = self.client.post(note_url(self.report_b, "reopen"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.report_b.refresh_from_db()
        self.assertIsNone(self.report_b.submitted_at)
        self.assertIn(fourth, self.report_b.proposals.all())
        response = self.submit(self.report_b)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.instance(fourth).status, WorkflowStepInstanceStatuses.COMPLETED
        )

    def rank(self, note, proposal, rank, user=None):
        self.client.force_authenticate(user or note.author)
        return self.client.post(
            RANKINGS_URL,
            {
                "round": self.round.uuid.hex,
                "meeting_note": note.uuid.hex,
                "proposal": proposal.uuid.hex,
                "rank": rank,
                "final_score": "4.2",
            },
            format="json",
        )

    def test_author_ranks_the_proposals_their_report_covers(self):
        response = self.rank(self.report_a, self.first, 1)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["meeting_note_title"], self.report_a.title)
        # Ranks are unique within a report, not across the round.
        self.assertEqual(
            self.rank(self.report_a, self.second, 1).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.rank(self.report_b, self.third, 1).status_code,
            status.HTTP_201_CREATED,
        )
        # Nor does a report's rank take one of the overall ranking.
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            RANKINGS_URL,
            {"round": self.round.uuid.hex, "proposal": self.first.uuid.hex, "rank": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.client.get(RANKINGS_URL, {"round_uuid": self.round.uuid.hex})
        self.assertEqual(len(response.data), 1)
        response = self.client.get(
            RANKINGS_URL,
            {
                "round_uuid": self.round.uuid.hex,
                "meeting_note_uuid": self.report_a.uuid.hex,
            },
        )
        self.assertEqual(len(response.data), 1)

    def test_author_does_not_rank_an_uncovered_proposal_or_another_report(self):
        self.assertEqual(
            self.rank(self.report_a, self.third, 1).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.rank(self.report_b, self.third, 1, user=self.chair_a).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.rank(self.report_a, self.first, 1, user=self.manager).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_ranking_is_fixed_once_the_report_is_submitted(self):
        self.rank(self.report_a, self.first, 1)
        self.answer(self.report_a)
        self.submit(self.report_a)
        self.assertEqual(
            self.rank(self.report_a, self.second, 2).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        ranking = models.ProposalRanking.objects.get(meeting_note=self.report_a)
        self.client.force_authenticate(self.chair_a)
        response = self.client.patch(
            "http://testserver"
            + reverse("proposal-ranking-detail", kwargs={"uuid": ranking.uuid.hex}),
            {"rank": 3},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RoundReportReadTest(RoundStepTestBase):
    def setUp(self):
        super().setUp()
        self.open_all()
        self.report_a = self.report_of(self.chair_a)

    def reads(self, user, note=None):
        self.client.force_authenticate(user)
        return (
            self.client.get(note_url(note or self.report_a)).status_code
            == status.HTTP_200_OK
        )

    def test_managers_and_the_author_read_a_report(self):
        self.assertTrue(self.reads(self.manager))
        self.assertTrue(self.reads(self.chair_a))
        self.assertTrue(self.reads(structure_factories.UserFactory(is_staff=True)))

    def test_other_panel_members_and_chairs_do_not(self):
        self.assertFalse(self.reads(self.chair_b))
        self.assertFalse(self.reads(self.outsider))

    def test_applicant_never_reads_a_report_on_their_proposal(self):
        self.assertFalse(self.reads(self.first.created_by))
        self.client.force_authenticate(self.first.created_by)
        response = self.client.get(NOTES_URL)
        self.assertEqual(response.data, [])

    def test_a_conflict_on_one_covered_proposal_hides_the_whole_report(self):
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.manager),
            proposal=self.second,
            status=COIStatuses.RECUSED,
        )
        self.assertFalse(self.reads(self.manager))

    def test_a_later_step_reading_the_round_step_reads_its_report(self):
        self.decision.order = 3
        self.decision.save(update_fields=["order"])
        later = models.CallWorkflowStep.objects.create(
            call=self.call,
            key="final",
            kind=StepKinds.ASSESSMENT,
            name="Final",
            order=2,
            outcome_set=OutcomeSets.DONE,
            completer=ResponsibleRoles.PANEL_MEMBER,
        )
        later.input_steps.set([self.meeting])
        instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.first,
            call_step=later,
            step="final",
            order=2,
            status=WorkflowStepInstanceStatuses.ACTIVE,
        )
        instance.input_steps.set([self.meeting])
        self.assertTrue(self.reads(self.outsider))
        self.assertFalse(self.reads(self.first.created_by))


class RoundStepDueDateTest(RoundStepTestBase):
    def set_due(self, due_at, step=None):
        self.client.force_authenticate(self.manager)
        url = factories.RoundFactory.get_url(
            self.call, self.round, "set_workflow_step_due_date"
        )
        return self.client.post(
            url,
            {
                "step": (step or self.meeting).key,
                "due_at": due_at.isoformat() if due_at else None,
            },
            format="json",
        )

    def test_due_date_moves_the_deadline_of_every_proposal_at_the_step(self):
        self.open_all()
        instance = self.instance(self.first)
        instance.sent_notifications = ["deadline_approaching:1", "step_started"]
        instance.save(update_fields=["sent_notifications"])
        due = timezone.now() + timedelta(days=3)
        response = self.set_due(due)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        for proposal in (self.first, self.second, self.third):
            self.assertEqual(self.instance(proposal).deadline, due)
        self.assertEqual(self.instance(self.first).sent_notifications, ["step_started"])
        meeting = next(
            row for row in response.data if row["step_uuid"] == self.meeting.uuid.hex
        )
        self.assertEqual(meeting["proposals_at_step"], 3)
        self.assertEqual(meeting["reports_total"], 2)

    def test_due_date_of_an_ordinary_step_applies_on_reaching_it(self):
        due = timezone.now() + timedelta(days=2)
        response = self.set_due(due, step=self.decision)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # The check is active now, and keeps its own deadline.
        self.open_all()
        report = self.report_of(self.chair_b)
        self.answer(report)
        self.submit(report)
        self.assertEqual(self.instance(self.third, self.decision).deadline, due)

    def test_due_date_in_the_past_is_refused(self):
        response = self.set_due(timezone.now() - timedelta(days=1))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reminders_go_to_draft_authors_once(self):
        self.open_all()
        # Due tomorrow by the calendar, as the reminders count days: an hour
        # past a day from now is the day after tomorrow once it is past 23:00.
        tomorrow = timezone.now() + timedelta(days=1)
        self.set_due(tomorrow.replace(hour=23, minute=59, second=0, microsecond=0))
        with mock.patch(AUTHORS_MAIL) as authors_mail:
            self.assertEqual(tasks.send_round_report_reminders(), 2)
            self.assertEqual(tasks.send_round_report_reminders(), 0)
        authors_mail.assert_called_once()


class AdvanceRoundTest(RoundStepTestBase):
    def advance(self, user=None, step=None):
        self.client.force_authenticate(user or self.manager)
        url = factories.RoundFactory.get_url(
            self.call, self.round, "advance_workflow_step"
        )
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, {"step": (step or self.check).key})

    def test_moves_on_every_proposal_parked_on_the_step(self):
        self.pass_check(self.first)
        self.pass_check(self.second)
        response = self.advance()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            {row["proposal_uuid"] for row in response.data["advanced"]},
            {self.first.uuid.hex, self.second.uuid.hex},
        )
        for proposal in (self.first, self.second):
            self.assertEqual(
                self.instance(proposal).status, WorkflowStepInstanceStatuses.ACTIVE
            )
        self.assertEqual(self.advance().data["advanced"], [])

    def test_step_that_moves_on_by_itself_is_refused(self):
        response = self.advance(step=self.meeting)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_call_managers_advance(self):
        self.pass_check(self.first)
        self.assertEqual(
            self.advance(user=self.chair_a).status_code, status.HTTP_403_FORBIDDEN
        )


class PanelChairRoundCompletionTest(RoundStepTestBase):
    """A decision run once per round that the call's panel chair takes."""

    def setUp(self):
        super().setUp()
        self.meeting.contributor_source = ContributorSources.NONE
        self.meeting.kind = StepKinds.DECISION
        self.meeting.outcome_set = OutcomeSets.APPROVAL
        self.meeting.completer = ResponsibleRoles.PANEL_CHAIR
        self.meeting.completer_proposal_role = None
        self.meeting.checklist = None
        self.meeting.save()
        self.call.panel_chair = self.chair_a
        self.call.save(update_fields=["panel_chair"])
        self.open_all()

    def complete(self, user):
        self.client.force_authenticate(user)
        url = factories.RoundFactory.get_url(
            self.call, self.round, "complete_workflow_step"
        )
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                url, {"step": self.meeting.key, "outcome": "approved"}, format="json"
            )

    def test_no_reports_for_a_step_without_contributors(self):
        self.assertFalse(
            models.RoundMeetingNote.objects.filter(call_step=self.meeting).exists()
        )

    def test_panel_chair_completes_it_for_the_round(self):
        response = self.complete(self.chair_a)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["completed"]), 3)

    def test_another_panel_member_cannot(self):
        self.assertEqual(
            self.complete(self.chair_b).status_code, status.HTTP_403_FORBIDDEN
        )


class RoundTasksTest(RoundStepTestBase):
    def needs(self, user):
        return {(task.needed, task.target_uuid) for task in my_tasks.tasks_of(user)}

    def test_manager_is_asked_to_open_and_authors_to_write(self):
        for proposal in (self.first, self.second, self.third):
            self.pass_check(proposal)
        with transaction.atomic():
            for proposal in (self.first, self.second, self.third):
                workflow_service.advance_step(proposal, acting_user=self.manager)
        self.assertIn(
            (my_tasks.Needs.OPEN_ROUND_STEP, self.round.uuid.hex),
            self.needs(self.manager),
        )
        self.assertEqual(self.needs(self.chair_a), set())
        with mock.patch(AUTHORS_MAIL), mock.patch(MANAGERS_MAIL):
            self.open()
        self.assertNotIn(
            (my_tasks.Needs.OPEN_ROUND_STEP, self.round.uuid.hex),
            self.needs(self.manager),
        )
        self.assertEqual(
            self.needs(self.chair_a),
            {
                (
                    my_tasks.Needs.WRITE_ROUND_REPORT,
                    self.report_of(self.chair_a).uuid.hex,
                )
            },
        )


class RoundReportChecklistTest(RoundStepTestBase):
    def test_readers_read_the_authors_answers(self):
        self.open_all()
        report = self.report_of(self.chair_a)
        self.answer(report)
        self.client.force_authenticate(self.manager)
        response = self.client.get(note_url(report, "checklist"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        answers = [
            question["existing_answer"]["answer_data"]
            for question in response.data["questions"]
            if question["existing_answer"]
        ]
        self.assertEqual(answers, ["Discussed."])


class ReviewDeadlineTest(RoundStepTestBase):
    """A review is due when the step it contributes to is: the round's due
    date for the step, else the step's duration; the round's review duration
    only for a review whose step has no deadline."""

    def setUp(self):
        super().setUp()
        self.review_step = self.add_step(
            "scientific_review",
            StepKinds.ASSESSMENT,
            3,
            outcome_set=OutcomeSets.REVIEWED,
            contributor_source=ContributorSources.ASSIGNED_REVIEWERS,
            completer=ResponsibleRoles.CALL_MANAGER,
            duration_in_days=20,
        )
        self.round.review_duration_in_days = 5
        self.round.save(update_fields=["review_duration_in_days"])
        self.instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.first,
            call_step=self.review_step,
            step=self.review_step.key,
            order=3,
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
            deadline=timezone.now() + timedelta(days=20),
        )
        self.review = factories.ReviewFactory(
            proposal=self.first, state=models.Review.States.IN_REVIEW
        )

    def link(self):
        # A review taken on joins the proposal's review step as its slot.
        self.assertEqual(self.review.contribution.proposal_step, self.instance)
        self.review.refresh_from_db()

    def without_step_deadline(self):
        models.ProposalWorkflowStepInstance.objects.filter(pk=self.instance.pk).update(
            deadline=None
        )
        self.review.refresh_from_db()

    def test_review_is_due_with_its_step(self):
        self.link()
        self.assertEqual(self.review.review_end_date, self.instance.deadline)
        due = models.Review.objects.with_deadline().filter(pk=self.review.pk)
        self.assertTrue(due.filter(review_deadline=self.instance.deadline).exists())

    def test_round_due_date_moves_the_review_deadline(self):
        self.link()
        due = timezone.now() + timedelta(days=2)
        round_steps.set_due_date(self.round, self.review_step, due)
        self.review.refresh_from_db()
        self.assertEqual(self.review.review_end_date, due)

    def test_review_expires_at_its_step_deadline_not_the_round_duration(self):
        self.link()
        # Past the round's five days, within the step's twenty.
        models.Review.objects.filter(pk=self.review.pk).update(
            created=timezone.now() - timedelta(days=10)
        )
        tasks.expired_reviews_should_be_cancelled()
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.IN_REVIEW)
        models.ProposalWorkflowStepInstance.objects.filter(pk=self.instance.pk).update(
            deadline=timezone.now() - timedelta(minutes=1)
        )
        tasks.expired_reviews_should_be_cancelled()
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.REJECTED)

    def test_without_a_step_deadline_the_round_duration_still_applies(self):
        self.without_step_deadline()
        self.assertEqual(
            self.review.review_end_date, self.review.created + timedelta(days=5)
        )
        models.Review.objects.filter(pk=self.review.pk).update(
            created=timezone.now() - timedelta(days=6)
        )
        tasks.expired_reviews_should_be_cancelled()
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, models.Review.States.REJECTED)

    def test_without_either_there_is_no_deadline(self):
        self.without_step_deadline()
        self.round.review_duration_in_days = None
        self.round.save(update_fields=["review_duration_in_days"])
        self.review.refresh_from_db()
        self.assertIsNone(self.review.review_end_date)
        self.assertFalse(
            models.Review.objects.with_deadline().filter(pk=self.review.pk).exists()
        )
