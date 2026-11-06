"""Post-award runs of an accepted proposal: extension requests and the final
report.

An accepted proposal walks the call's extension steps each time its team asks
to extend the allocation, and its closing steps once, for the final report.
A run never allocates, rejects or cancels: the proposal stays accepted. An
approved extension moves the project's and resources' end dates at once and
puts changed limits through an update order the provider approves as usual,
but only on offerings whose team accepted the extension.
"""

import datetime
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from waldur_core.permissions.fixtures import OfferingRole, ProjectRole
from waldur_core.structure.models import ProjectEndDateChangeRequest
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import (
    change_requests,
    contributions,
    models,
    round_lifecycle,
    runs,
    tasks,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    ChangeRequestParts,
    ContributionOutcomes,
    ContributorSources,
    OnNegativeOutcomes,
    OutcomeSets,
    PostAwardStatuses,
    ProposalRunStates,
    ProposalStates,
    ResponsibleRoles,
    RoundLifecycleStates,
    StepContributionStatuses,
    StepKinds,
    UndecidedAtRoundCompletion,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.models import (
    CallWorkflowStep,
    ProposalChangeRequest,
    ProposalRun,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_awarded_resources import (
    AwardedResourcesBase,
)

TODAY = datetime.date(2030, 6, 1)


def _step(call, key, kind, outcome_set, completer, phase, **values):
    return CallWorkflowStep.objects.create(
        call=call,
        key=key,
        name=key.replace("_", " ").capitalize(),
        kind=kind,
        outcome_set=outcome_set,
        completer=completer,
        phase=phase,
        order=CallWorkflowStep.next_order(call.pk),
        **values,
    )


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class PostAwardBase(AwardedResourcesBase):
    """An accepted proposal whose award provisioned one resource, on a call
    with an extension (request, per-offering assessment, decision) and a
    closing (final report, approval)."""

    def setUp(self):
        super().setUp()
        self.provider = structure_factories.UserFactory()
        self.offering.add_user(self.provider, OfferingRole.MANAGER)
        # Requested, but never awarded: no part in the post-award steps.
        self.bystander = structure_factories.UserFactory()
        self.other_offering.add_user(self.bystander, OfferingRole.MANAGER)
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.other_requested_offering,
            resource=None,
            limits={"cpu": 5},
        )

        self.request_step = _step(
            self.call,
            "extension_request",
            StepKinds.APPLICANT_ACTION,
            OutcomeSets.SUBMITTED,
            ResponsibleRoles.APPLICANT,
            WorkflowPhases.EXTENSION,
            project_end_offset_days=30,
            duration_in_days=14,
        )
        self.assessment_step = _step(
            self.call,
            "extension_assessment",
            StepKinds.ASSESSMENT,
            OutcomeSets.PER_OFFERING,
            ResponsibleRoles.OFFERING_MANAGER,
            WorkflowPhases.EXTENSION,
            contributor_source=ContributorSources.REQUESTED_OFFERING_TEAM,
            per_requested_offering=True,
            on_negative=OnNegativeOutcomes.CONTINUE,
            duration_in_days=14,
        )
        self.decision_step = _step(
            self.call,
            "extension_decision",
            StepKinds.DECISION,
            OutcomeSets.APPROVAL,
            ResponsibleRoles.CALL_MANAGER,
            WorkflowPhases.EXTENSION,
            duration_in_days=14,
        )
        self.report_step = _step(
            self.call,
            "final_report",
            StepKinds.APPLICANT_ACTION,
            OutcomeSets.SUBMITTED,
            ResponsibleRoles.APPLICANT,
            WorkflowPhases.CLOSING,
            project_end_offset_days=0,
            duration_in_days=30,
        )
        self.approval_step = _step(
            self.call,
            "final_report_approval",
            StepKinds.DECISION,
            OutcomeSets.APPROVAL,
            ResponsibleRoles.CALL_MANAGER,
            WorkflowPhases.CLOSING,
        )

        # Only the first request is awarded; the second one is not.
        self._start_allocation()
        for awarded in self.proposal.awarded_resources.filter(
            requested_offering=self.other_requested_offering
        ):
            awarded.delete()
        self._decide()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.project = self.proposal.project
        self.project.end_date = TODAY + datetime.timedelta(days=90)
        self.project.save()
        self.resource = self._awarded().resource
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.end_date = self.project.end_date
        self.resource.limits = {"cpu": 100}
        self.resource.save()
        marketplace_models.Order.objects.filter(resource=self.resource).update(
            state=OrderStates.DONE
        )
        self.member = structure_factories.UserFactory()
        self.project.add_user(self.member, ProjectRole.ADMIN)

        patcher = mock.patch(
            "waldur_mastermind.proposal.runs._today", return_value=TODAY
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # Helpers

    def _url(self, action):
        return factories.ProposalFactory.get_url(self.proposal, action=action)

    def _post(self, user, action, data=None):
        self.client.force_authenticate(user)
        return self.client.post(self._url(action), data or {}, format="json")

    def _run_url(self, run):
        return self._url("runs") + run.uuid.hex + "/"

    def _patch_run(self, user, run, data):
        self.client.force_authenticate(user)
        return self.client.patch(self._run_url(run), data, format="json")

    def _request_extension(self, data=None):
        response = self._post(
            self.applicant,
            "request_extension",
            data if data is not None else {"requested_end_date": "2030-12-31"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return ProposalRun.objects.get(uuid=response.data["uuid"])

    def _active(self, run):
        return run.step_instances.get(status=WorkflowStepInstanceStatuses.ACTIVE)

    def _complete(self, user, instance, outcome, expected=status.HTTP_200_OK):
        response = self._post(
            user,
            "complete_workflow_step",
            {"step_uuid": instance.uuid.hex, "outcome": outcome},
        )
        self.assertEqual(response.status_code, expected, response.data)
        self.proposal.refresh_from_db()
        return response

    def _assess(self, run, outcome=ContributionOutcomes.ACCEPTED):
        instance = self._active(run)
        self.assertEqual(instance.call_step, self.assessment_step)
        for contribution in instance.contributions.all():
            contributions.submit(contribution, self.provider, outcome=outcome)
        workflow_service.complete_from_contributions(
            self.proposal, instance, self.provider
        )
        self.proposal.refresh_from_db()
        return instance

    def _extend(self, granted_limits=None):
        """Request an extension, assess it, and approve it."""
        run = self._request_extension(
            {
                "requested_end_date": "2030-12-31",
                "requested_limits": {self.resource.uuid.hex: {"cpu": 200}},
            }
        )
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        self._assess(run)
        if granted_limits is not None:
            response = self._patch_run(
                self.call_manager, run, {"granted_limits": granted_limits}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self._complete(
            self.call_manager, self._active(run), WorkflowStepOutcomes.APPROVED
        )
        run.refresh_from_db()
        return run


class PathTest(PostAwardBase):
    def test_submission_walks_the_evaluation_steps_only(self):
        proposal = factories.ProposalFactory(round=self.proposal.round)

        workflow_service.create_step_instances(proposal)

        phases = set(
            proposal.workflow_step_instances.values_list("call_step__phase", flat=True)
        )
        self.assertEqual(phases, {WorkflowPhases.EVALUATION})

    def test_the_accepted_proposal_has_no_run_until_asked(self):
        self.assertFalse(self.proposal.runs.exists())
        self.assertIsNone(runs.post_award_status(self.proposal))


class RequestExtensionTest(PostAwardBase):
    def test_the_proposal_team_opens_an_extension_run(self):
        run = self._request_extension()

        self.proposal.refresh_from_db()
        self.assertEqual(run.phase, WorkflowPhases.EXTENSION)
        self.assertEqual(run.state, ProposalRunStates.OPEN)
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        active = self._active(run)
        self.assertEqual(active.call_step, self.request_step)
        self.assertEqual(self.proposal.current_step, active)
        self.assertEqual(
            runs.post_award_status(self.proposal),
            PostAwardStatuses.EXTENSION_REQUESTED,
        )

    def test_nobody_but_the_proposal_team_asks(self):
        for user in (self.call_manager, self.member, self.provider):
            response = self._post(user, "request_extension", {})
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                user.username,
            )
        self.assertFalse(self.proposal.runs.exists())

    def test_one_open_run_at_a_time(self):
        self._request_extension()

        response = self._post(self.applicant, "request_extension", {})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_no_request_after_the_window(self):
        self.project.end_date = TODAY + datetime.timedelta(days=29)
        self.project.save()

        response = self._post(self.applicant, "request_extension", {})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("until", response.data["detail"])

    def test_no_request_on_a_proposal_not_accepted(self):
        self.proposal.state = ProposalStates.REJECTED
        self.proposal.save()

        response = self._post(self.applicant, "request_extension", {})

        self.assertIn(
            response.status_code,
            (status.HTTP_409_CONFLICT, status.HTTP_404_NOT_FOUND),
        )
        self.assertFalse(self.proposal.runs.exists())

    def test_a_prepaid_resource_cannot_be_asked_for(self):
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="prepaid_cpu", is_prepaid=True
        )

        response = self._post(
            self.applicant,
            "request_extension",
            {"requested_limits": {self.resource.uuid.hex: {"cpu": 200}}},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(self.resource.uuid.hex, str(response.data))

    def test_limits_name_only_the_proposals_resources(self):
        other = marketplace_factories.ResourceFactory(offering=self.offering)

        response = self._post(
            self.applicant,
            "request_extension",
            {"requested_limits": {other.uuid.hex: {"cpu": 200}}},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_request_says_what_it_asks_for_before_it_is_submitted(self):
        run = self._request_extension({})

        self._complete(
            self.applicant,
            self._active(run),
            WorkflowStepOutcomes.SUBMITTED,
            expected=status.HTTP_400_BAD_REQUEST,
        )

        response = self._patch_run(
            self.applicant, run, {"requested_end_date": "2030-12-31"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        self.assertEqual(self._active(run).call_step, self.assessment_step)

    def test_who_writes_what_on_the_run(self):
        run = self._request_extension()

        # The call manager does not write what the applicant asks for.
        response = self._patch_run(
            self.call_manager, run, {"requested_end_date": "2031-01-31"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # The applicant never writes what is granted.
        response = self._patch_run(
            self.applicant, run, {"granted_end_date": "2031-01-31"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # Nor is the grant written before the decision runs.
        response = self._patch_run(
            self.call_manager, run, {"granted_end_date": "2031-01-31"}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_the_end_date_lies_after_the_current_end(self):
        response = self._post(
            self.applicant,
            "request_extension",
            {"requested_end_date": self.project.end_date.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AssessmentTest(PostAwardBase):
    def _submitted_run(self):
        run = self._request_extension()
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        return run

    def test_one_slot_per_awarded_and_provisioned_offering(self):
        run = self._submitted_run()

        instance = self._active(run)
        self.assertEqual(
            list(
                instance.contributions.values_list(
                    "requested_offering__offering", flat=True
                )
            ),
            [self.offering.pk],
        )

    def test_a_requested_but_not_awarded_offering_has_no_part(self):
        run = self._submitted_run()
        instance = self._active(run)

        self.assertFalse(contributions.may_contribute(self.bystander, instance))
        self.assertTrue(contributions.may_contribute(self.provider, instance))
        response = self._post(
            self.bystander,
            "complete_workflow_step",
            {
                "step_uuid": instance.uuid.hex,
                "outcome": WorkflowStepOutcomes.FEASIBLE,
            },
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_two_extensions_have_their_own_steps_and_work(self):
        first = self._extend()
        second = self._request_extension({"requested_end_date": "2031-06-30"})
        self._complete(
            self.applicant, self._active(second), WorkflowStepOutcomes.SUBMITTED
        )

        first_slots = set(
            first.step_instances.get(
                call_step=self.assessment_step
            ).contributions.values_list("pk", flat=True)
        )
        second_slots = set(
            self._active(second).contributions.values_list("pk", flat=True)
        )
        self.assertTrue(first_slots)
        self.assertTrue(second_slots)
        self.assertFalse(first_slots & second_slots)
        self.assertEqual(
            self.proposal.workflow_step_instances.filter(
                call_step=self.request_step
            ).count(),
            2,
        )


class ApprovalTest(PostAwardBase):
    def test_an_approved_extension_moves_the_end_dates_and_orders_the_limits(self):
        run = self._extend()

        self.proposal.refresh_from_db()
        self.project.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual(run.state, ProposalRunStates.APPROVED)
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertIsNone(self.proposal.current_step)
        new_end = datetime.date(2030, 12, 31)
        self.assertEqual(self.project.end_date, new_end)
        self.assertEqual(self.resource.end_date, new_end)
        # Decided already: nobody is asked to review an end date request.
        self.assertFalse(
            ProjectEndDateChangeRequest.objects.filter(project=self.project).exists()
        )
        self.assertFalse(
            marketplace_models.ResourceEndDateChangeRequest.objects.filter(
                resource=self.resource
            ).exists()
        )
        order = marketplace_models.Order.objects.get(
            resource=self.resource, type=OrderTypes.UPDATE
        )
        self.assertEqual(order.limits["cpu"], 200)
        self.assertEqual(order.consumer_reviewed_by, self.call_manager)
        self.assertEqual(run.applied_changes["orders"][0]["order_uuid"], order.uuid.hex)
        self.assertEqual(
            run.applied_changes["project_end_date"]["end_date"], "2030-12-31"
        )
        self.assertEqual(
            run.applied_changes["resource_end_dates"][0]["resource_uuid"],
            self.resource.uuid.hex,
        )
        self.assertEqual(
            runs.post_award_status(self.proposal), PostAwardStatuses.PROJECT_EXTENDED
        )

    def test_the_decision_edits_what_is_granted(self):
        run = self._extend(granted_limits={self.resource.uuid.hex: {"cpu": 150}})

        order = marketplace_models.Order.objects.get(
            resource=self.resource, type=OrderTypes.UPDATE
        )
        self.assertEqual(order.limits["cpu"], 150)
        self.assertEqual(run.granted_limits[self.resource.uuid.hex]["cpu"], 150)

    def test_a_rejected_assessment_leaves_the_offerings_resources_alone(self):
        run = self._request_extension(
            {
                "requested_end_date": "2030-12-31",
                "requested_limits": {self.resource.uuid.hex: {"cpu": 200}},
            }
        )
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        self._assess(run, ContributionOutcomes.REJECTED)
        self._complete(
            self.call_manager, self._active(run), WorkflowStepOutcomes.APPROVED
        )

        run.refresh_from_db()
        self.project.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual(run.state, ProposalRunStates.APPROVED)
        # The project is the managing organisation's own: it moves.
        self.assertEqual(self.project.end_date, datetime.date(2030, 12, 31))
        # The resource's offering never accepted: nothing changes on it.
        self.assertEqual(self.resource.end_date, TODAY + datetime.timedelta(days=90))
        self.assertFalse(
            marketplace_models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.UPDATE
            ).exists()
        )
        self.assertEqual(
            run.applied_changes["skipped"][0]["resource_uuid"], self.resource.uuid.hex
        )

    def test_the_offerings_change_request_options_are_bypassed_only_after_its_assessment(
        self,
    ):
        # The offering takes no change requests at all.
        self.offering.plugin_options = {}
        self.offering.save()

        self._extend()

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.end_date, datetime.date(2030, 12, 31))

    def test_a_slot_the_call_manager_filled_does_not_speak_for_the_provider(self):
        run = self._request_extension(
            {
                "requested_end_date": "2030-12-31",
                "requested_limits": {self.resource.uuid.hex: {"cpu": 200}},
            }
        )
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        instance = self._active(run)
        for contribution in instance.contributions.all():
            contributions.submit(
                contribution, self.call_manager, outcome=ContributionOutcomes.ACCEPTED
            )
        workflow_service.complete_from_contributions(
            self.proposal, instance, self.call_manager
        )
        self._complete(
            self.call_manager, self._active(run), WorkflowStepOutcomes.APPROVED
        )

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.end_date, TODAY + datetime.timedelta(days=90))
        self.assertFalse(
            marketplace_models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.UPDATE
            ).exists()
        )

    def test_a_declined_extension_changes_nothing(self):
        run = self._request_extension()
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        self._assess(run)
        self._complete(
            self.call_manager, self._active(run), WorkflowStepOutcomes.DECLINED
        )

        run.refresh_from_db()
        self.project.refresh_from_db()
        self.proposal.refresh_from_db()
        self.assertEqual(run.state, ProposalRunStates.REJECTED)
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual(self.project.end_date, TODAY + datetime.timedelta(days=90))
        self.assertFalse(ProjectEndDateChangeRequest.objects.exists())

    def test_rejecting_at_a_run_step_closes_the_run_only(self):
        run = self._request_extension()
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )

        response = self._post(
            self.call_manager,
            "reject_workflow_step",
            {"step_uuid": self._active(run).uuid.hex, "reason": "No"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        run.refresh_from_db()
        self.proposal.refresh_from_db()
        self.assertEqual(run.state, ProposalRunStates.REJECTED)
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)

    def test_a_lapsed_request_withdraws_the_run(self):
        run = self._request_extension()
        self._active(run).__class__.objects.filter(pk=self._active(run).pk).update(
            deadline=timezone.now() - datetime.timedelta(minutes=1)
        )

        tasks.mark_expired_workflow_steps()

        run.refresh_from_db()
        self.proposal.refresh_from_db()
        self.assertEqual(run.state, ProposalRunStates.WITHDRAWN)
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)

    def test_the_withdrawal_is_decided_under_the_proposals_lock(self):
        """The open run and its active step are read only once the proposal
        is locked, so a submission of the request committing meanwhile is
        waited for."""
        run = self._request_extension()
        calls = []
        lock = models.Proposal.objects.select_for_update
        open_run_of = runs.open_run_of

        def locking(*args, **kwargs):
            calls.append("lock")
            return lock(*args, **kwargs)

        def reading(*args, **kwargs):
            calls.append("read")
            return open_run_of(*args, **kwargs)

        with (
            mock.patch.object(models.Proposal.objects, "select_for_update", locking),
            mock.patch.object(runs, "open_run_of", reading),
        ):
            response = self._post(self.applicant, "withdraw_run", {})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(calls[0], "lock")
        run.refresh_from_db()
        self.assertEqual(run.state, ProposalRunStates.WITHDRAWN)

    def test_the_applicant_withdraws_while_writing_the_call_manager_later(self):
        run = self._request_extension()
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )

        response = self._post(self.applicant, "withdraw_run", {})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        response = self._post(self.call_manager, "withdraw_run", {})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        run.refresh_from_db()
        self.assertEqual(run.state, ProposalRunStates.WITHDRAWN)
        self.assertFalse(
            run.step_instances.filter(
                status=WorkflowStepInstanceStatuses.ACTIVE
            ).exists()
        )
        self.assertFalse(
            run.step_instances.get(call_step=self.assessment_step)
            .contributions.filter(status__in=StepContributionStatuses.OPEN)
            .exists()
        )


class FinalReportTest(PostAwardBase):
    def _due(self):
        return runs.due_final_report_proposals(today=TODAY)

    def test_the_final_report_opens_at_the_project_end(self):
        self.assertEqual(self._due(), [])
        self.project.end_date = TODAY
        self.project.save()

        self.assertEqual([p.pk for p in self._due()], [self.proposal.pk])
        tasks.open_due_final_reports()

        run = self.proposal.runs.get()
        self.assertEqual(run.phase, WorkflowPhases.CLOSING)
        self.assertIsNone(run.opened_by)
        self.assertEqual(self._active(run).call_step, self.report_step)
        self.proposal.refresh_from_db()
        self.assertEqual(
            runs.post_award_status(self.proposal),
            PostAwardStatuses.PENDING_FINAL_REPORT,
        )
        # Once only.
        tasks.open_due_final_reports()
        self.assertEqual(self.proposal.runs.count(), 1)

    def test_not_while_an_extension_is_open(self):
        self._request_extension()
        self.project.end_date = TODAY
        self.project.save()

        self.assertEqual(self._due(), [])

    def test_a_granted_extension_moves_the_due_date(self):
        self._extend()
        self.project.refresh_from_db()

        self.assertEqual(self._due(), [])
        self.assertEqual(
            runs.final_report_opens_on(self.proposal), datetime.date(2030, 12, 31)
        )

    def test_a_project_without_an_end_date_is_opened_by_hand(self):
        self.project.end_date = None
        self.project.save()
        self.assertEqual(self._due(), [])

        response = self._post(self.applicant, "open_final_report")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        response = self._post(self.call_manager, "open_final_report")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_an_overdue_report_stays_to_be_submitted_and_is_told_once(self):
        self._post(self.call_manager, "open_final_report")
        run = self.proposal.runs.get()
        report = self._active(run)
        report.__class__.objects.filter(pk=report.pk).update(
            deadline=timezone.now() - datetime.timedelta(days=1)
        )

        with mock.patch.object(
            workflow_service, "_announce", wraps=workflow_service._announce
        ) as announce:
            tasks.mark_expired_workflow_steps()
            tasks.mark_expired_workflow_steps()

        report.refresh_from_db()
        self.assertEqual(report.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIn(workflow_service.OVERDUE_LEDGER_KEY, report.sent_notifications)
        self.assertEqual(announce.call_count, 1)
        self._complete(self.applicant, report, WorkflowStepOutcomes.SUBMITTED)
        self.assertEqual(
            runs.post_award_status(self.proposal),
            PostAwardStatuses.FINAL_REPORT_SUBMITTED,
        )

    def test_an_approved_report_completes_the_project(self):
        self._post(self.call_manager, "open_final_report")
        run = self.proposal.runs.get()
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        self._complete(
            self.call_manager, self._active(run), WorkflowStepOutcomes.APPROVED
        )

        self.proposal.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(run.state, ProposalRunStates.APPROVED)
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertIsNotNone(self.proposal.completed_at)
        self.assertEqual(self.proposal.completed_by, self.call_manager)
        self.assertEqual(
            runs.post_award_status(self.proposal),
            PostAwardStatuses.PROJECT_COMPLETED,
        )
        response = self._post(self.applicant, "request_extension", {})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


class VisibilityTest(PostAwardBase):
    def test_reviewers_see_nothing_of_the_runs(self):
        run = self._request_extension()
        reviewer = self.fixture.reviewer_1

        self.client.force_authenticate(reviewer)
        response = self.client.get(self._url("runs"))
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        response = self.client.get(self._url("workflow_states"))
        if response.status_code == status.HTTP_200_OK:
            self.assertNotIn(
                str(self._active(run).uuid.hex),
                [item["uuid"] for item in response.data],
            )

    def test_the_applicant_reads_the_grant_once_decided(self):
        run = self._request_extension()
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        self._assess(run)
        response = self._patch_run(
            self.call_manager, run, {"granted_end_date": "2030-11-30"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.force_authenticate(self.applicant)
        response = self.client.get(self._url("runs"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data[0]["granted_end_date"])

        self._complete(
            self.call_manager, self._active(run), WorkflowStepOutcomes.APPROVED
        )
        response = self.client.get(self._url("runs"))
        self.assertEqual(str(response.data[0]["granted_end_date"]), "2030-11-30")

    def test_the_call_manager_and_the_provider_read_the_runs(self):
        self._request_extension()
        for user in (self.call_manager, self.provider):
            self.client.force_authenticate(user)
            response = self.client.get(self._url("runs"))
            self.assertEqual(response.status_code, status.HTTP_200_OK, user.username)
            self.assertEqual(len(response.data), 1)


class ConfigurationTest(PostAwardBase):
    def test_post_award_steps_settle_no_award_and_cancel_nothing(self):
        step = CallWorkflowStep(
            call=self.call,
            key="x",
            kind=StepKinds.DECISION,
            outcome_set=OutcomeSets.APPROVAL,
            phase=WorkflowPhases.EXTENSION,
            on_negative=OnNegativeOutcomes.CANCEL,
        )
        with self.assertRaises(Exception) as caught:
            step.clean()
        self.assertIn("on_negative", str(caught.exception))

        step.on_negative = OnNegativeOutcomes.REJECT
        step.is_allocation_decision = True
        with self.assertRaises(Exception) as caught:
            step.clean()
        self.assertIn("is_allocation_decision", str(caught.exception))

    def test_the_evaluation_does_not_read_a_post_award_step(self):
        with self.assertRaises(Exception) as caught:
            CallWorkflowStep.check_input_steps(
                None, self.call.pk, None, [self.request_step]
            )
        self.assertIn("phase", str(caught.exception))
        # The extension reads the evaluation.
        CallWorkflowStep.check_input_steps(
            None,
            self.call.pk,
            None,
            [self.call.workflow_steps.get(key="administrative_check")],
            WorkflowPhases.CLOSING,
        )

    def test_a_phase_opens_with_the_applicant_and_an_extension_ends_with_a_decision(
        self,
    ):
        self.assertEqual(CallWorkflowStep.phase_structure_errors(self.call.pk), [])

        self.decision_step.is_enabled = False
        self.decision_step.save()

        errors = CallWorkflowStep.phase_structure_errors(self.call.pk)
        self.assertEqual(len(errors), 1)
        self.assertIn("extension ends with a decision", errors[0])

    def test_steps_of_different_phases_never_share_a_stage(self):
        self.assessment_step.parallel_with_previous = True
        self.assessment_step.save()
        self.report_step.parallel_with_previous = True
        self.report_step.save()

        stages = CallWorkflowStep.stages(
            list(self.call.workflow_steps.order_by("order", "id"))
        )

        self.assertEqual(stages[self.assessment_step.pk], self.request_step.order)
        self.assertEqual(stages[self.report_step.pk], self.report_step.order)


class ListingTest(PostAwardBase):
    def _list(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(factories.ProposalFactory.get_list_url(), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [item["uuid"] for item in response.data]

    def test_the_call_manager_filters_by_post_award_status(self):
        self._request_extension()

        self.assertEqual(
            self._list(
                self.call_manager,
                post_award_status=PostAwardStatuses.EXTENSION_REQUESTED,
            ),
            [self.proposal.uuid.hex],
        )
        self.assertEqual(
            self._list(
                self.call_manager,
                post_award_status=PostAwardStatuses.PENDING_FINAL_REPORT,
            ),
            [],
        )
        self.assertEqual(
            self._list(self.call_manager, run_phase=WorkflowPhases.EXTENSION),
            [self.proposal.uuid.hex],
        )

    def test_the_list_shows_the_status_to_the_call_manager_only(self):
        self._request_extension()

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(
            response.data["post_award_status"], PostAwardStatuses.EXTENSION_REQUESTED
        )
        self.assertEqual(response.data["open_run_phase"], WorkflowPhases.EXTENSION)
        self.assertEqual(
            str(response.data["extension_requestable_until"]),
            str(TODAY + datetime.timedelta(days=60)),
        )

    def test_the_final_report_is_openable_only_while_the_call_has_closing_steps(self):
        self.client.force_authenticate(self.call_manager)
        url = factories.ProposalFactory.get_url(self.proposal)
        self.assertTrue(self.client.get(url).data["final_report_openable"])

        self._post(self.call_manager, "open_final_report")
        self.assertFalse(self.client.get(url).data["final_report_openable"])

        self.proposal.runs.all().delete()
        CallWorkflowStep.phase_steps(
            self.proposal.round.call_id, WorkflowPhases.CLOSING
        ).update(is_enabled=False)
        self.assertFalse(self.client.get(url).data["final_report_openable"])

    def test_overdue_final_reports_are_listed(self):
        self._post(self.call_manager, "open_final_report")
        report = self._active(self.proposal.runs.get())

        self.assertEqual(self._list(self.call_manager, final_report_overdue=True), [])
        report.__class__.objects.filter(pk=report.pk).update(
            deadline=timezone.now() - datetime.timedelta(days=1)
        )
        self.assertEqual(
            self._list(self.call_manager, final_report_overdue=True),
            [self.proposal.uuid.hex],
        )


class RoundCompletionTest(PostAwardBase):
    def test_completing_the_round_leaves_an_accepted_proposal_with_an_open_run(self):
        run = self._request_extension()
        call_round = self.proposal.round
        call_round.cutoff_time = timezone.now() - datetime.timedelta(days=1)
        call_round.lifecycle_state = RoundLifecycleStates.RESULTS_PUBLISHED
        call_round.undecided_at_round_completion = UndecidedAtRoundCompletion.REJECT
        call_round.save()

        round_lifecycle.complete(call_round, self.call_manager)

        self.proposal.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual(run.state, ProposalRunStates.OPEN)
        self.assertEqual(self._active(run).status, WorkflowStepInstanceStatuses.ACTIVE)


class RunChangeRequestTest(PostAwardBase):
    """The decision on an extension or final report sends it back to the
    proposal's team for changes, as during the evaluation, but only the
    run's own parts unlock."""

    def setUp(self):
        super().setUp()
        CallWorkflowStep.objects.filter(
            pk__in=[self.decision_step.pk, self.approval_step.pk]
        ).update(allows_change_requests=True)

    def _report_at_approval(self):
        self._post(self.call_manager, "open_final_report")
        run = self.proposal.runs.get()
        report = self._active(run)
        self._complete(self.applicant, report, WorkflowStepOutcomes.SUBMITTED)
        return run, report, self._active(run)

    def _request_changes(self, user, instance, parts, **extra):
        return self._post(
            user,
            "request_changes",
            {
                "step_uuid": instance.uuid.hex,
                "message": "Please revise",
                "parts": parts,
                **extra,
            },
        )

    def test_the_final_report_goes_back_to_the_team_and_returns(self):
        run, report, approval = self._report_at_approval()

        response = self._request_changes(
            self.call_manager, approval, [ChangeRequestParts.RUN_FORM]
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(contributions.run_form_unlocked(report))
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        # The approval waits for the changes.
        self._complete(
            self.call_manager,
            approval,
            WorkflowStepOutcomes.APPROVED,
            expected=status.HTTP_400_BAD_REQUEST,
        )

        response = self._post(self.applicant, "submit_changes")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(contributions.run_form_unlocked(report))
        self._complete(self.call_manager, approval, WorkflowStepOutcomes.APPROVED)
        self.proposal.refresh_from_db()
        self.assertIsNotNone(self.proposal.completed_at)

    def test_only_the_runs_parts_unlock(self):
        _, _, approval = self._report_at_approval()

        response = self._request_changes(self.call_manager, approval, ["field:name"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # A final report asks for no end date.
        response = self._request_changes(
            self.call_manager, approval, [ChangeRequestParts.RUN_REQUEST]
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(
            self._url("unlockable_parts"), {"step_uuid": approval.uuid.hex}
        )
        self.assertEqual(response.data["parts"], [ChangeRequestParts.RUN_FORM])

    def test_the_team_never_asks(self):
        _, _, approval = self._report_at_approval()

        response = self._request_changes(
            self.applicant, approval, [ChangeRequestParts.RUN_FORM]
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_extension_request_reopens_what_it_asks_for(self):
        run = self._request_extension()
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        self._assess(run)
        decision = self._active(run)

        # Locked once submitted.
        response = self._patch_run(
            self.applicant, run, {"requested_end_date": "2031-03-31"}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        response = self._request_changes(
            self.call_manager,
            decision,
            [ChangeRequestParts.RUN_REQUEST, ChangeRequestParts.RUN_FORM],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self._patch_run(
            self.applicant, run, {"requested_end_date": "2031-03-31"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # The proposal's own content stays locked.
        self.assertFalse(
            change_requests.applicant_may_edit(self.proposal, "field:name")
        )

        self._post(self.applicant, "submit_changes")
        response = self._patch_run(
            self.applicant, run, {"requested_end_date": "2031-04-30"}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_a_lapsed_request_returns_the_step_to_its_actor(self):
        _, _, approval = self._report_at_approval()
        self._request_changes(
            self.call_manager,
            approval,
            [ChangeRequestParts.RUN_FORM],
            due_at=(timezone.now() + datetime.timedelta(days=1)).isoformat(),
        )
        ProposalChangeRequest.objects.update(
            due_at=timezone.now() - datetime.timedelta(minutes=1)
        )

        tasks.lapse_change_requests()

        change_request = ProposalChangeRequest.objects.get()
        self.assertIsNotNone(change_request.closed_at)
        approval.refresh_from_db()
        self.assertEqual(approval.status, WorkflowStepInstanceStatuses.ACTIVE)
        self._complete(self.call_manager, approval, WorkflowStepOutcomes.APPROVED)

    def test_withdrawing_the_run_calls_off_the_changes(self):
        run = self._request_extension()
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        self._assess(run)
        self._request_changes(
            self.call_manager, self._active(run), [ChangeRequestParts.RUN_REQUEST]
        )

        response = self._post(self.call_manager, "withdraw_run", {})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNotNone(ProposalChangeRequest.objects.get().closed_at)
