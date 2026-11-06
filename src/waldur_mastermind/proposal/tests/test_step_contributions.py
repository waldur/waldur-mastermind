"""A workflow step's work collected as contributions.

One contribution per requested offering on a per-offering technical
assessment, filled by that offering's team alone; one per person elsewhere.
The step's form is answered per contribution, its gates count this step's
contributions, and a per-offering step derives its outcome from theirs.
"""

from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.core import models as core_models
from waldur_core.permissions.fixtures import CallRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import contributions, models, utils, workflow_service
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    ContributionOutcomes,
    ContributorSources,
    OnNegativeOutcomes,
    OutcomeSets,
    ProposalStates,
    StepContributionStatuses,
    StepKinds,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories, fixtures

SUBMITTED = StepContributionStatuses.SUBMITTED


class PerOfferingAssessmentBase(test.APITestCase):
    """A proposal requesting two offerings, managed by different people, at
    a technical assessment split per requested offering."""

    on_negative = OnNegativeOutcomes.CONTINUE

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call_manager = self.fixture.call_manager

        self.checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.WORKFLOW_STEP
        )
        self.question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            required=True,
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
        )

        models.CallWorkflowStep.objects.filter(
            call=self.call, key="administrative_check"
        ).update(is_enabled=False)
        self.call_step = models.CallWorkflowStep.objects.get(
            call=self.call, key="technical_assessment"
        )
        self.call_step.is_enabled = True
        self.call_step.contributor_source = ContributorSources.REQUESTED_OFFERING_TEAM
        self.call_step.per_requested_offering = True
        self.call_step.outcome_set = OutcomeSets.PER_OFFERING
        self.call_step.on_negative = self.on_negative
        self.call_step.checklist = self.checklist
        self.call_step.save()

        self.proposal = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.SUBMITTED
        )
        fixtures.author_as_manager(self.proposal)
        self.manager_a, self.offering_a, self.requested_a = self._request_offering()
        self.manager_b, self.offering_b, self.requested_b = self._request_offering()

        workflow_service.create_step_instances(self.proposal)
        workflow_service.activate_first_step(self.proposal)
        self.proposal.refresh_from_db()
        self.instance = self.proposal.current_step

    def _request_offering(self):
        manager = structure_factories.UserFactory()
        offering = marketplace_factories.OfferingFactory()
        offering.add_user(manager, OfferingRole.MANAGER)
        requested = factories.RequestedOfferingFactory(
            call=self.call, offering=offering, created_by=self.fixture.owner
        )
        factories.RequestedResourceFactory(
            proposal=self.proposal, requested_offering=requested
        )
        return manager, offering, requested

    def _slot(self, requested_offering):
        return self.instance.contributions.get(requested_offering=requested_offering)

    def _url(self, action):
        return factories.ProposalFactory.get_url(self.proposal, action=action)

    def _answer(self, user, contribution, text="Fits"):
        self.client.force_authenticate(user)
        return self.client.post(
            self._url("submit-step-checklist-answers")
            + f"?step_uuid={self.call_step.uuid.hex}"
            + f"&contribution_uuid={contribution.uuid.hex}",
            [{"question_uuid": str(self.question.uuid), "answer_data": text}],
            format="json",
        )

    def _submit(self, user, contribution, outcome, conditions=""):
        self.client.force_authenticate(user)
        return self.client.post(
            self._url("submit_contribution"),
            {
                "contribution_uuid": contribution.uuid.hex,
                "outcome": outcome,
                "conditions": conditions,
            },
            format="json",
        )


class PerOfferingSlotsTest(PerOfferingAssessmentBase):
    def test_activation_creates_one_slot_per_requested_offering(self):
        slots = self.instance.contributions.all()
        self.assertEqual(slots.count(), 2)
        self.assertEqual(
            {slot.requested_offering_id for slot in slots},
            {self.requested_a.id, self.requested_b.id},
        )
        for slot in slots:
            self.assertIsNone(slot.contributor_id)
            self.assertEqual(slot.status, StepContributionStatuses.PENDING)

    def test_each_team_fills_only_its_own_slot(self):
        slot_a = self._slot(self.requested_a)
        slot_b = self._slot(self.requested_b)

        self.assertEqual(
            self._answer(self.manager_a, slot_a).status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self._answer(self.manager_a, slot_b).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self._submit(
                self.manager_a, slot_b, ContributionOutcomes.ACCEPTED
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_answers_of_one_slot_never_appear_in_the_other(self):
        slot_a = self._slot(self.requested_a)
        slot_b = self._slot(self.requested_b)
        self._answer(self.manager_a, slot_a, "Partition A fits")

        self.assertIsNone(slot_b.get_checklist_completion(self.checklist))
        self.client.force_authenticate(self.manager_b)
        response = self.client.get(
            self._url("step-checklist"),
            {"step_uuid": self.call_step.uuid.hex, "include_all": "true"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["questions"][0]["existing_answer"])

    def test_a_manager_of_one_offering_resolves_their_slot_without_naming_it(self):
        self.client.force_authenticate(self.manager_b)
        response = self.client.post(
            self._url("submit-step-checklist-answers")
            + f"?step_uuid={self.call_step.uuid.hex}",
            [{"question_uuid": str(self.question.uuid), "answer_data": "B fits"}],
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        completion = self._slot(self.requested_b).get_checklist_completion(
            self.checklist
        )
        self.assertEqual(completion.answers.get().answer_data, "B fits")

    def test_contributions_list_names_the_offerings(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(
            self._url("contributions"), {"step_uuid": self.call_step.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {row["offering_name"] for row in response.data},
            {self.offering_a.name, self.offering_b.name},
        )
        self.assertTrue(all(row["can_fill"] for row in response.data))

    def _visible_offerings(self, user, action, **params):
        self.client.force_authenticate(user)
        response = self.client.get(self._url(action), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return sorted(
            row.get("offering_name") or row.get("user_full_name")
            for row in response.data
        )

    def test_a_team_reads_only_its_own_slot_blind_or_not(self):
        slot_b = self._slot(self.requested_b)
        self._answer(self.manager_b, slot_b, "B is fine")
        for blind in (False, True):
            self.call_step.blind_review = blind
            self.call_step.save()
            self.assertEqual(
                self._visible_offerings(self.manager_a, "contributions"),
                [self.offering_a.name],
            )
            self.assertEqual(
                self._visible_offerings(
                    self.manager_a,
                    "step-checklist-responses",
                    step_uuid=self.call_step.uuid.hex,
                ),
                [self.offering_a.name],
            )
            response = self.client.get(
                self._url("step-checklist"),
                {
                    "step_uuid": self.call_step.uuid.hex,
                    "contribution_uuid": slot_b.uuid.hex,
                },
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_team_reads_only_its_own_offerings_outcome(self):
        for requested, manager in (
            (self.requested_a, self.manager_a),
            (self.requested_b, self.manager_b),
        ):
            slot = self._slot(requested)
            self._answer(manager, slot)
            self._submit(manager, slot, ContributionOutcomes.ACCEPTED)
        self.assertEqual(
            self._visible_offerings(self.manager_a, "offering_assessments"),
            [self.offering_a.name],
        )
        self.assertEqual(
            self._visible_offerings(self.call_manager, "offering_assessments"),
            sorted([self.offering_a.name, self.offering_b.name]),
        )

    def test_files_of_a_slot_follow_who_reads_it(self):
        from waldur_core.checklist import media_access

        slot_a = self._slot(self.requested_a)
        self._answer(self.manager_a, slot_a)
        completion = slot_a.get_checklist_completion(self.checklist)
        teammate = structure_factories.UserFactory()
        self.offering_a.add_user(teammate, OfferingRole.MANAGER)
        for user, allowed in (
            (teammate, True),
            (self.call_manager, True),
            (self.manager_b, False),
        ):
            self.assertEqual(
                media_access._user_can_access_completion(user, completion),
                allowed,
                user,
            )

    def test_a_completer_who_does_not_contribute_reads_every_slot(self):
        self.call_step.completer = "panel_member"
        self.call_step.save()
        panelist = structure_factories.UserFactory()
        self.call.add_user(panelist, CallRole.PANEL_MEMBER)
        self.assertEqual(
            self._visible_offerings(panelist, "contributions"),
            sorted([self.offering_a.name, self.offering_b.name]),
        )


class ContributorIdentityTest(PerOfferingAssessmentBase):
    """The applicant does not learn who assessed their proposal."""

    def setUp(self):
        super().setUp()
        self.call_step.applicant_visible = True
        self.call_step.save()
        self.slot = self._slot(self.requested_a)
        self._answer(self.manager_a, self.slot, "Partition A fits")
        self._submit(self.manager_a, self.slot, ContributionOutcomes.ACCEPTED)
        self.applicant = self.proposal.created_by

    def _form(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            self._url("step-checklist"),
            {
                "step_uuid": self.call_step.uuid.hex,
                "contribution_uuid": self.slot.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data["questions"][0]["existing_answer"]

    def test_the_applicant_reads_the_answers_but_not_who_gave_them(self):
        answer = self._form(self.applicant)
        self.assertEqual(answer["answer_data"], "Partition A fits")
        self.assertNotIn("user", answer)
        self.assertNotIn("user_name", answer)
        response = self.client.get(self._url("contributions"))
        self.assertEqual(response.data[0]["contributor_uuid"], None)
        self.assertNotEqual(
            response.data[0]["contributor_full_name"], self.manager_a.full_name
        )

    def test_the_call_may_reveal_who_gave_them(self):
        self.call.reviewer_identity_visible_to_submitters = True
        self.call.save()
        answer = self._form(self.applicant)
        self.assertEqual(answer["user_name"], self.manager_a.full_name)

    def test_the_call_manager_always_sees_who_gave_them(self):
        answer = self._form(self.call_manager)
        self.assertEqual(answer["user_name"], self.manager_a.full_name)


class PerOfferingOutcomeTest(PerOfferingAssessmentBase):
    def _fill_both(self, outcome_a, outcome_b, conditions_b=""):
        slot_a = self._slot(self.requested_a)
        slot_b = self._slot(self.requested_b)
        self._answer(self.manager_a, slot_a)
        self._answer(self.manager_b, slot_b)
        response_a = self._submit(self.manager_a, slot_a, outcome_a)
        response_b = self._submit(self.manager_b, slot_b, outcome_b, conditions_b)
        return response_a, response_b

    def test_conditional_acceptance_needs_its_conditions(self):
        slot = self._slot(self.requested_b)
        self._answer(self.manager_b, slot)
        response = self._submit(
            self.manager_b, slot, ContributionOutcomes.CONDITIONALLY_ACCEPTED
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_submitting_needs_the_required_answers(self):
        response = self._submit(
            self.manager_a, self._slot(self.requested_a), ContributionOutcomes.ACCEPTED
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_outcome_outside_the_set_is_refused(self):
        slot = self._slot(self.requested_a)
        self._answer(self.manager_a, slot)
        response = self._submit(self.manager_a, slot, "feasible")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_submitting_makes_the_slot_the_submitters(self):
        slot = self._slot(self.requested_a)
        self._answer(self.manager_a, slot)
        response = self._submit(self.manager_a, slot, ContributionOutcomes.ACCEPTED)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["step_completed"])
        slot.refresh_from_db()
        self.assertEqual(slot.status, SUBMITTED)
        self.assertEqual(slot.contributor, self.manager_a)
        self.assertEqual(
            self._submit(
                self.manager_a, slot, ContributionOutcomes.REJECTED
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_rejected_partition_continues_to_the_next_step(self):
        response_a, response_b = self._fill_both(
            ContributionOutcomes.REJECTED,
            ContributionOutcomes.CONDITIONALLY_ACCEPTED,
            "Only with the GPU partition upgrade",
        )
        self.assertEqual(response_a.status_code, status.HTTP_200_OK)
        self.assertEqual(response_b.status_code, status.HTTP_200_OK)
        self.assertTrue(response_b.data["step_completed"])

        self.instance.refresh_from_db()
        self.proposal.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(self.instance.outcome, WorkflowStepOutcomes.INFEASIBLE)
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertNotEqual(self.proposal.current_step_id, self.instance.pk)
        self.assertEqual(
            self.proposal.current_step.status, WorkflowStepInstanceStatuses.ACTIVE
        )

    def test_all_accepted_is_feasible(self):
        self._fill_both(
            ContributionOutcomes.ACCEPTED,
            ContributionOutcomes.CONDITIONALLY_ACCEPTED,
            "Within quota",
        )
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.outcome, WorkflowStepOutcomes.FEASIBLE)

    def test_offering_assessments_show_outcomes_and_conditions(self):
        self._fill_both(
            ContributionOutcomes.REJECTED,
            ContributionOutcomes.CONDITIONALLY_ACCEPTED,
            "Only with the GPU partition upgrade",
        )
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self._url("offering_assessments"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_offering = {row["offering_name"]: row for row in response.data}
        self.assertEqual(
            by_offering[self.offering_a.name]["outcome"], ContributionOutcomes.REJECTED
        )
        self.assertEqual(
            by_offering[self.offering_b.name]["conditions"],
            "Only with the GPU partition upgrade",
        )

    def test_applicant_does_not_see_assessments_of_a_hidden_step(self):
        self._fill_both(ContributionOutcomes.ACCEPTED, ContributionOutcomes.ACCEPTED)
        self.client.force_authenticate(self.proposal.created_by)
        response = self.client.get(self._url("offering_assessments"))
        self.assertEqual(response.data, [])

    def test_completing_by_hand_must_match_the_derived_outcome(self):
        slot = self._slot(self.requested_a)
        self._answer(self.manager_a, slot)
        self._submit(self.manager_a, slot, ContributionOutcomes.REJECTED)
        with self.assertRaises(ValueError):
            workflow_service.complete_step(
                self.proposal,
                self.instance,
                WorkflowStepOutcomes.FEASIBLE,
                "",
                self.call_manager,
            )

    def test_step_waits_for_every_offering(self):
        slot = self._slot(self.requested_a)
        self._answer(self.manager_a, slot)
        self._submit(self.manager_a, slot, ContributionOutcomes.ACCEPTED)
        with self.assertRaisesRegex(ValueError, "still to be assessed"):
            workflow_service.complete_step(
                self.proposal,
                self.instance,
                WorkflowStepOutcomes.FEASIBLE,
                "",
                self.call_manager,
            )

    def test_withdrawn_slot_no_longer_holds_the_step(self):
        slot_b = self._slot(self.requested_b)
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self._url("decline_contribution"),
            {"contribution_uuid": slot_b.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], StepContributionStatuses.WITHDRAWN)

        slot_a = self._slot(self.requested_a)
        self._answer(self.manager_a, slot_a)
        response = self._submit(self.manager_a, slot_a, ContributionOutcomes.ACCEPTED)
        self.assertTrue(response.data["step_completed"])

    def test_withdrawing_the_last_open_slot_completes_the_step(self):
        slot_a = self._slot(self.requested_a)
        self._answer(self.manager_a, slot_a)
        self._submit(self.manager_a, slot_a, ContributionOutcomes.ACCEPTED)
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            self._url("decline_contribution"),
            {"contribution_uuid": self._slot(self.requested_b).uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(self.instance.outcome, WorkflowStepOutcomes.FEASIBLE)

    def test_a_submitted_slot_is_no_longer_fillable(self):
        slot_a = self._slot(self.requested_a)
        self._answer(self.manager_a, slot_a)
        self._submit(self.manager_a, slot_a, ContributionOutcomes.ACCEPTED)
        for user in (self.manager_a, self.call_manager):
            self.client.force_authenticate(user)
            response = self.client.get(self._url("contributions"))
            row = next(r for r in response.data if r["uuid"] == slot_a.uuid.hex)
            self.assertFalse(row["can_fill"])

    def test_submitting_once_the_step_is_over_says_so(self):
        slot_a = self._slot(self.requested_a)
        self._answer(self.manager_a, slot_a)
        workflow_service.expire_step(self.instance)
        response = self._submit(self.manager_a, slot_a, ContributionOutcomes.ACCEPTED)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no longer be submitted", str(response.data["detail"]))

    def test_the_applicant_waits_for_a_held_decision(self):
        self.call_step.applicant_visible = True
        self.call_step.save()
        self._fill_both(ContributionOutcomes.ACCEPTED, ContributionOutcomes.ACCEPTED)
        self.client.force_authenticate(self.proposal.created_by)
        self.assertEqual(
            len(self.client.get(self._url("offering_assessments")).data), 2
        )
        models.Proposal.objects.filter(pk=self.proposal.pk).update(decision_held=True)
        self.assertEqual(self.client.get(self._url("offering_assessments")).data, [])

    def test_a_team_cannot_decline_its_offering_slot(self):
        self.client.force_authenticate(self.manager_a)
        response = self.client.post(
            self._url("decline_contribution"),
            {"contribution_uuid": self._slot(self.requested_a).uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_expiry_expires_open_slots(self):
        workflow_service.expire_step(self.instance)
        self.assertFalse(
            self.instance.contributions.filter(
                status__in=StepContributionStatuses.OPEN
            ).exists()
        )


class ContributionQueryCountTest(PerOfferingAssessmentBase):
    """Listing contributions costs the same however many there are."""

    def _queries(self, user, action, **params):
        self.client.force_authenticate(user)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self._url(action), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return len(queries), len(response.data)

    def _measure(self):
        step = {"step_uuid": self.call_step.uuid.hex}
        return [
            self._queries(self.call_manager, "contributions"),
            self._queries(self.call_manager, "step-checklist-responses", **step),
            self._queries(self.call_manager, "offering_assessments"),
            self._queries(self.manager_a, "contributions")[0],
        ]

    def _answer_every_slot(self, managers):
        for requested, manager in managers:
            self._answer(manager, self._slot(requested))

    def test_listing_does_not_grow_with_the_slots(self):
        managers = [
            (self.requested_a, self.manager_a),
            (self.requested_b, self.manager_b),
        ]
        self._answer_every_slot(managers)
        self._measure()  # warm caches shared by every request
        before = self._measure()

        for _ in range(4):
            manager, _offering, requested = self._request_offering()
            managers.append((requested, manager))
        contributions.create_contributions(self.instance)
        self._answer_every_slot(managers[2:])
        after = self._measure()

        for (count_before, rows_before), (count_after, rows_after) in zip(
            before[:3], after[:3]
        ):
            self.assertEqual((rows_before, rows_after), (2, 6))
            self.assertEqual(count_before, count_after)
        self.assertEqual(before[3], after[3])


class RejectOnNegativeTest(PerOfferingAssessmentBase):
    on_negative = OnNegativeOutcomes.REJECT

    def test_rejected_partition_rejects_the_proposal(self):
        for requested, manager, outcome in (
            (self.requested_a, self.manager_a, ContributionOutcomes.ACCEPTED),
            (self.requested_b, self.manager_b, ContributionOutcomes.REJECTED),
        ):
            slot = self._slot(requested)
            self._answer(manager, slot)
            self._submit(manager, slot, outcome)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.REJECTED)


class AcceptOrRejectOnlyTest(PerOfferingAssessmentBase):
    """A step that takes only a yes or a no from each partition."""

    def setUp(self):
        super().setUp()
        self.call_step.allows_conditional_acceptance = False
        self.call_step.save()

    def test_a_conditional_acceptance_is_refused(self):
        slot = self._slot(self.requested_b)
        self._answer(self.manager_b, slot)
        response = self._submit(
            self.manager_b,
            slot,
            ContributionOutcomes.CONDITIONALLY_ACCEPTED,
            "Only with the GPU partition upgrade",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        slot.refresh_from_db()
        self.assertNotEqual(slot.status, SUBMITTED)

    def test_the_step_offers_accepted_or_rejected(self):
        self.assertEqual(
            self.call_step.contribution_outcomes,
            (ContributionOutcomes.ACCEPTED, ContributionOutcomes.REJECTED),
        )
        slot = self._slot(self.requested_b)
        self._answer(self.manager_b, slot)
        response = self._submit(self.manager_b, slot, ContributionOutcomes.ACCEPTED)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class SameFormTwoStepsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.checklist = checklist_factories.ChecklistFactory()
        self.question = checklist_factories.QuestionFactory(checklist=self.checklist)
        self.first = factories.CallWorkflowStepFactory(
            call=self.call,
            key="individual-report",
            kind=StepKinds.ASSESSMENT,
            name="Individual report",
            outcome_set=OutcomeSets.DONE,
            completer="call_manager",
            checklist=self.checklist,
            contributor_source="call_role:call_manager",
        )
        self.second = factories.CallWorkflowStepFactory(
            call=self.call,
            key="consolidated-report",
            kind=StepKinds.ASSESSMENT,
            name="Consolidated report",
            outcome_set=OutcomeSets.DONE,
            completer="call_manager",
            checklist=self.checklist,
            contributor_source="call_role:call_manager",
        )
        self.proposal = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.IN_REVIEW
        )
        workflow_service.create_step_instances(self.proposal)
        for instance in self.proposal.workflow_step_instances.filter(
            call_step__in=[self.first, self.second]
        ):
            instance.status = WorkflowStepInstanceStatuses.ACTIVE
            instance.started_at = timezone.now()
            # One active step per proposal; the first is checked while active.
            if instance.call_step_id == self.second.pk:
                instance.status = WorkflowStepInstanceStatuses.PENDING
            instance.save()

    def _post(self, call_step, text):
        self.client.force_authenticate(self.fixture.call_manager)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="submit-step-checklist-answers"
            )
            + f"?step_uuid={call_step.uuid.hex}",
            [{"question_uuid": str(self.question.uuid), "answer_data": text}],
            format="json",
        )

    def test_reading_the_form_makes_no_contribution(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="step-checklist"),
            {"step_uuid": self.first.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.data["questions"][0]["existing_answer"])
        self.assertFalse(
            models.StepContribution.objects.filter(
                proposal_step__proposal=self.proposal
            ).exists()
        )
        self.assertEqual(
            self._post(self.first, "Individual").status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            models.StepContribution.objects.filter(
                proposal_step__proposal=self.proposal
            ).count(),
            1,
        )

    def test_completing_the_step_withdraws_its_open_contributions(self):
        self._post(self.first, "Individual")
        instance = self.proposal.workflow_step_instances.get(call_step=self.first)
        workflow_service.complete_step(
            self.proposal,
            instance,
            WorkflowStepOutcomes.DONE,
            "",
            self.fixture.call_manager,
        )
        self.assertEqual(
            instance.contributions.get().status, StepContributionStatuses.WITHDRAWN
        )

    def test_two_steps_keep_separate_answers(self):
        self.assertEqual(
            self._post(self.first, "Individual").status_code, status.HTTP_200_OK
        )
        # The second step's form is answered once it runs.
        self.proposal.workflow_step_instances.filter(call_step=self.first).update(
            status=WorkflowStepInstanceStatuses.COMPLETED
        )
        self.proposal.workflow_step_instances.filter(call_step=self.second).update(
            status=WorkflowStepInstanceStatuses.ACTIVE, started_at=timezone.now()
        )
        self.assertEqual(
            self._post(self.second, "Consolidated").status_code, status.HTTP_200_OK
        )
        answers = {
            contribution.proposal_step.call_step_id: contribution.get_checklist_completion(
                self.checklist
            )
            .answers.get()
            .answer_data
            for contribution in models.StepContribution.objects.filter(
                proposal_step__proposal=self.proposal
            )
        }
        self.assertEqual(
            answers, {self.first.pk: "Individual", self.second.pk: "Consolidated"}
        )


class ReviewerPrivacyTest(test.APITestCase):
    """Reviewers read their own review contribution, not each other's."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.WORKFLOW_STEP
        )
        self.question = checklist_factories.QuestionFactory(
            checklist=self.checklist,
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
        )
        self.review_step = models.CallWorkflowStep.objects.get(
            call=self.call, key="expert_review"
        )
        self.review_step.is_enabled = True
        self.review_step.checklist = self.checklist
        self.review_step.save()
        self.proposal = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.SUBMITTED
        )
        workflow_service.create_step_instances(self.proposal)
        self.instance = self.proposal.workflow_step_instances.get(step="expert_review")
        self.instance.status = WorkflowStepInstanceStatuses.ACTIVE
        self.instance.save()
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.current_step = self.instance
        self.proposal.save()
        self.first, self.second = (
            self._reviewer(),
            self._reviewer(),
        )

    def _reviewer(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.REVIEWER)
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=user,
            state=models.Review.States.IN_REVIEW,
        )
        return user

    def _url(self, action):
        return factories.ProposalFactory.get_url(self.proposal, action=action)

    def _contribution(self, user):
        return self.instance.contributions.get(contributor=user)

    def test_a_reviewer_reads_only_their_own_contribution(self):
        self.client.force_authenticate(self.first)
        response = self.client.post(
            self._url("submit-step-checklist-answers")
            + f"?step_uuid={self.review_step.uuid.hex}",
            [{"question_uuid": str(self.question.uuid), "answer_data": "Strong"}],
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.force_authenticate(self.second)
        response = self.client.get(self._url("contributions"))
        self.assertEqual(
            [row["uuid"] for row in response.data],
            [self._contribution(self.second).uuid.hex],
        )
        response = self.client.get(
            self._url("step-checklist"),
            {
                "step_uuid": self.review_step.uuid.hex,
                "contribution_uuid": self._contribution(self.first).uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.get(
            self._url("step-checklist"), {"step_uuid": self.review_step.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.data["questions"][0]["existing_answer"])
        response = self.client.get(
            self._url("step-checklist-responses"),
            {"step_uuid": self.review_step.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_call_manager_reads_every_review_contribution(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(
            self._url("step-checklist-responses"),
            {"step_uuid": self.review_step.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)


class ReviewContributionGateTest(test.APITestCase):
    """The review step counts the reviews of its own contributions."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.review_step = models.CallWorkflowStep.objects.get(
            call=self.call, key="expert_review"
        )
        self.review_step.is_enabled = True
        self.review_step.min_contributions = 2
        self.review_step.save()
        self.proposal = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.SUBMITTED
        )
        workflow_service.create_step_instances(self.proposal)
        self.instance = self.proposal.workflow_step_instances.get(step="expert_review")
        self.instance.status = WorkflowStepInstanceStatuses.ACTIVE
        self.instance.save()
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.current_step = self.instance
        self.proposal.save()

    def _review(self, state=models.Review.States.SUBMITTED):
        return factories.ReviewFactory(proposal=self.proposal, state=state)

    def test_every_review_gets_a_contribution_on_the_review_step(self):
        review = self._review(models.Review.States.IN_REVIEW)
        contribution = review.contribution
        self.assertEqual(contribution.proposal_step, self.instance)
        self.assertEqual(contribution.status, StepContributionStatuses.ACCEPTED)
        review.state = models.Review.States.SUBMITTED
        review.save()
        contribution.refresh_from_db()
        self.assertEqual(contribution.status, SUBMITTED)

    def test_min_contributions_counts_submitted_reviews(self):
        self._review()
        self._review(models.Review.States.IN_REVIEW)
        with self.assertRaisesRegex(ValueError, "1 of 2 required reviews"):
            workflow_service.complete_step(
                self.proposal,
                self.instance,
                WorkflowStepOutcomes.REVIEWED,
                "",
                self.fixture.call_manager,
            )
        self._review()
        workflow_service.complete_step(
            self.proposal,
            self.instance,
            WorkflowStepOutcomes.REVIEWED,
            "",
            self.fixture.call_manager,
        )

    def test_a_contribution_without_a_review_does_not_count(self):
        self._review()
        factories.StepContributionFactory(proposal_step=self.instance, status=SUBMITTED)
        with self.assertRaises(ValueError):
            workflow_service.complete_step(
                self.proposal,
                self.instance,
                WorkflowStepOutcomes.REVIEWED,
                "",
                self.fixture.call_manager,
            )

    def test_max_contributions_refuses_one_more(self):
        self.review_step.min_contributions = None
        self.review_step.max_contributions = 1
        self.review_step.save()
        self.instance.call_step.refresh_from_db()
        manager = self.fixture.call_manager
        first = contributions.resolve_contribution(manager, self.instance)
        contributions.submit(first, manager)
        other = factories.StepContributionFactory(proposal_step=self.instance)
        with self.assertRaisesRegex(contributions.ContributionError, "at most 1"):
            contributions.submit(other, other.contributor)

    def test_more_reviews_than_the_maximum_do_not_hold_the_step(self):
        self.review_step.min_contributions = None
        self.review_step.max_contributions = 1
        self.review_step.save()
        self.instance.call_step.refresh_from_db()
        self._review()
        self._review()
        workflow_service.complete_step(
            self.proposal,
            self.instance,
            WorkflowStepOutcomes.REVIEWED,
            "",
            self.fixture.call_manager,
        )
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowStepInstanceStatuses.COMPLETED)

    def test_declined_is_never_blocked(self):
        panel = models.CallWorkflowStep.objects.get(call=self.call, key="panel_review")
        panel.min_contributions = 5
        panel.save()
        instance = self.proposal.workflow_step_instances.get(step="panel_review")
        workflow_service._enforce_step_gates(
            self.proposal, instance, WorkflowStepOutcomes.DECLINED, panel
        )

    def test_step_without_contributors_counts_the_proposals_reviews(self):
        panel = models.CallWorkflowStep.objects.get(call=self.call, key="panel_review")
        panel.min_contributions = 1
        panel.save()
        instance = self.proposal.workflow_step_instances.get(step="panel_review")
        with self.assertRaises(ValueError):
            workflow_service._enforce_step_gates(
                self.proposal, instance, WorkflowStepOutcomes.APPROVED, panel
            )
        self._review()
        workflow_service._enforce_step_gates(
            self.proposal, instance, WorkflowStepOutcomes.APPROVED, panel
        )

    def test_accepted_assignment_links_its_contribution_to_the_batch_step(self):
        second = factories.CallWorkflowStepFactory(
            call=self.call,
            key="second-review",
            kind=StepKinds.ASSESSMENT,
            name="Second review",
            outcome_set=OutcomeSets.REVIEWED,
            completer="reviewer",
            contributor_source=ContributorSources.ASSIGNED_REVIEWERS,
        )
        second_instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal, call_step=second, step=second.key
        )
        pool_entry = factories.CallReviewerPoolFactory(call=self.call)
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=pool_entry,
            call_step=second,
            status=AssignmentBatchStatuses.SENT,
        )
        item = models.AssignmentItem.objects.create(batch=batch, proposal=self.proposal)

        review = item.accept()

        contribution = models.StepContribution.objects.get(review=review)
        self.assertEqual(contribution.assignment_item, item)
        self.assertEqual(contribution.proposal_step, second_instance)

    def test_a_new_batch_defaults_to_the_review_step(self):
        batch = factories.AssignmentBatchFactory(call=self.call)
        self.assertEqual(batch.call_step, self.review_step)


class CallStepContributionConfigTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.state = "draft"
        self.call.save()
        self.step = models.CallWorkflowStep.objects.get(
            call=self.call, key="technical_assessment"
        )
        self.url = factories.CallWorkflowStepFactory.get_url(self.call, self.step)
        self.client.force_authenticate(self.fixture.staff)

    def _patch(self, payload):
        return self.client.patch(self.url, payload, format="json")

    def test_per_requested_offering_implies_the_offering_team(self):
        self.step.contributor_source = ContributorSources.NONE
        self.step.save()
        response = self._patch(
            {"per_requested_offering": True, "outcome_set": OutcomeSets.PER_OFFERING}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["contributor_source"],
            ContributorSources.REQUESTED_OFFERING_TEAM,
        )

    def test_per_offering_outcomes_need_a_slot_per_offering(self):
        response = self._patch({"outcome_set": OutcomeSets.PER_OFFERING})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_contributor_source_is_refused(self):
        for value in (
            "everyone",
            "call_role:owner",
            "proposal_role:",
            "proposal_role:lead",
            "proposal_role:12ab",
        ):
            response = self._patch({"contributor_source": value})
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, value)

    def test_proposal_role_source_is_accepted(self):
        for value in ("proposal_role:5", f"proposal_role:{'a' * 32}"):
            response = self._patch({"contributor_source": value})
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_role_holders_hook_gets_the_whole_source(self):
        self.step.contributor_source = "proposal_role:5"
        self.step.is_enabled = True
        self.step.save()
        proposal = factories.ProposalFactory(round=self.fixture.round)
        workflow_service.create_step_instances(proposal)
        instance = proposal.workflow_step_instances.get(call_step=self.step)
        with mock.patch.object(
            contributions,
            "proposal_role_holders",
            return_value=core_models.User.objects.none(),
        ) as holders:
            contributions.create_contributions(instance)
            contributions.may_contribute(self.fixture.staff, instance)
        for call in holders.call_args_list:
            self.assertEqual(call.args[1], "proposal_role:5")
        self.assertEqual(holders.call_count, 2)

    def test_min_reviewers_is_an_alias_of_min_contributions(self):
        response = self._patch({"min_reviewers": 3, "max_contributions": 4})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["min_contributions"], 3)
        self.assertEqual(response.data["min_reviewers"], 3)

    def test_conflicting_minimums_are_refused(self):
        response = self._patch({"min_reviewers": 2, "min_contributions": 3})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self._patch({"min_reviewers": 3, "min_contributions": 3})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["min_contributions"], 3)

    def test_max_below_min_is_refused(self):
        response = self._patch({"min_contributions": 3, "max_contributions": 2})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_continue_is_refused_on_the_allocation_decision(self):
        decision = models.CallWorkflowStep.allocation_decision_for(self.call)
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, decision),
            {"on_negative": OnNegativeOutcomes.CONTINUE},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_an_assessment_has_contributors(self):
        decision = models.CallWorkflowStep.allocation_decision_for(self.call)
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, decision),
            {"contributor_source": "call_role:reviewer"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_kinds_endpoint_lists_the_contribution_fields(self):
        response = self.client.get("/api/proposal-protected-calls/workflow_step_kinds/")
        assessment = next(kind for kind in response.data if kind["id"] == "assessment")
        for field in (
            "contributor_source",
            "per_requested_offering",
            "min_contributions",
            "max_contributions",
        ):
            self.assertIn(field, assessment["config_fields"])
        self.assertIn(
            OutcomeSets.PER_OFFERING, [s["id"] for s in assessment["outcome_sets"]]
        )

    def test_contributors_are_fixed_once_proposals_walk_the_step(self):
        proposal = factories.ProposalFactory(round=self.fixture.round)
        workflow_service.create_step_instances(proposal)
        response = self._patch({"contributor_source": "call_role:reviewer"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_copies_the_contribution_fields(self):
        self.step.contributor_source = ContributorSources.REQUESTED_OFFERING_TEAM
        self.step.per_requested_offering = True
        self.step.outcome_set = OutcomeSets.PER_OFFERING
        self.step.on_negative = OnNegativeOutcomes.CONTINUE
        self.step.min_contributions = 1
        self.step.max_contributions = 3
        self.step.save()
        copy = utils.duplicate_call(self.call, "Copy", self.fixture.staff)
        copied = models.CallWorkflowStep.objects.get(
            call=copy, key="technical_assessment"
        )
        for field in (
            "contributor_source",
            "per_requested_offering",
            "outcome_set",
            "on_negative",
            "min_contributions",
            "max_contributions",
        ):
            self.assertEqual(getattr(copied, field), getattr(self.step, field), field)


class AdoptProposalAnswersTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.IN_REVIEW
        )
        self.checklists = []
        for key in ("technical_assessment", "expert_review"):
            checklist = checklist_factories.ChecklistFactory()
            question = checklist_factories.QuestionFactory(checklist=checklist)
            models.CallWorkflowStep.objects.filter(call=self.call, key=key).update(
                is_enabled=True, checklist=checklist
            )
            self.checklists.append((checklist, question))
        workflow_service.create_step_instances(self.proposal)

    def _answer_on_the_proposal(self, checklist, question, user):
        completion = self.proposal.ensure_checklist_completion_for(checklist)
        checklist_factories.AnswerFactory(
            completion=completion, question=question, user=user
        )
        return completion

    def test_answers_of_a_deactivated_user_move(self):
        checklist, question = self.checklists[0]
        user = structure_factories.UserFactory(is_active=False)
        self._answer_on_the_proposal(checklist, question, user)

        self.assertEqual(contributions.adopt_proposal_answers(self.proposal), 1)
        contribution = models.StepContribution.objects.get(
            proposal_step__proposal=self.proposal
        )
        self.assertEqual(contribution.contributor_id, user.id)

    def test_a_proposal_moves_all_its_answers_or_none(self):
        user = structure_factories.UserFactory()
        completions = [
            self._answer_on_the_proposal(checklist, question, user)
            for checklist, question in self.checklists
        ]
        original = contributions._adopt_completion
        calls = []

        def fail_on_second(proposal, completion):
            calls.append(completion)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return original(proposal, completion)

        with mock.patch.object(
            contributions, "_adopt_completion", side_effect=fail_on_second
        ):
            with self.assertRaises(RuntimeError):
                contributions.adopt_proposal_answers(self.proposal)
        self.assertFalse(
            models.StepContribution.objects.filter(
                proposal_step__proposal=self.proposal
            ).exists()
        )
        for completion in completions:
            self.assertEqual(completion.answers.count(), 1)

    def test_a_form_shared_by_two_steps_reaches_both(self):
        # Both steps read the proposal's one completion of the form; the
        # second must still find the answers once they move.
        checklist, _ = self.checklists[0]
        required = checklist_factories.QuestionFactory(
            checklist=checklist, required=True
        )
        models.CallWorkflowStep.objects.filter(
            call=self.call, key="expert_review"
        ).update(checklist=checklist)
        user = structure_factories.UserFactory()
        self._answer_on_the_proposal(checklist, required, user)

        self.assertEqual(contributions.adopt_proposal_answers(self.proposal), 1)
        for key in ("technical_assessment", "expert_review"):
            instance = self.proposal.workflow_step_instances.get(call_step__key=key)
            contribution = instance.contributions.get(contributor=user)
            completion = contribution.get_checklist_completion(checklist)
            self.assertEqual(completion.answers.get().question, required, key)
            self.assertFalse(
                contributions.unanswered_required_questions(instance).exists(), key
            )
        self.assertIsNone(self.proposal.get_checklist_completion_for(checklist))


class ComplianceFormStepGateTest(test.APITestCase):
    """A step whose form is the call's compliance checklist reads the
    applicant's answers, which stay on the proposal."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.compliance = checklist_factories.ChecklistFactory()
        self.question = checklist_factories.QuestionFactory(
            checklist=self.compliance, required=True
        )
        self.call.compliance_checklist = self.compliance
        self.call.save()
        models.CallWorkflowStep.objects.filter(
            call=self.call, key="technical_assessment"
        ).update(is_enabled=True, checklist=self.compliance)
        self.proposal = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.IN_REVIEW
        )
        workflow_service.create_step_instances(self.proposal)
        self.instance = self.proposal.workflow_step_instances.get(
            call_step__key="technical_assessment"
        )

    def test_unanswered_until_the_applicant_answers(self):
        self.assertTrue(
            contributions.unanswered_required_questions(self.instance).exists()
        )

    def test_the_applicants_answers_satisfy_the_step(self):
        checklist_factories.AnswerFactory(
            completion=self.proposal.ensure_checklist_completion_for(self.compliance),
            question=self.question,
            user=self.proposal.created_by,
        )
        self.assertFalse(
            contributions.unanswered_required_questions(self.instance).exists()
        )


class AssignedReviewerSlotStandingTest(test.APITestCase):
    """On a step of assigned reviewers, a slot of the user's makes them one
    of its contributors only while it counts."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        step = factories.CallWorkflowStepFactory(
            call=self.fixture.call,
            key="second-review",
            kind=StepKinds.ASSESSMENT,
            name="Second review",
            outcome_set=OutcomeSets.REVIEWED,
            completer="reviewer",
            contributor_source=ContributorSources.ASSIGNED_REVIEWERS,
        )
        self.instance = factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal, call_step=step, step=step.key
        )
        self.user = structure_factories.UserFactory()
        self.slot = models.StepContribution.objects.create(
            proposal_step=self.instance, contributor=self.user
        )

    def test_an_open_slot_counts(self):
        self.assertTrue(contributions.may_contribute(self.user, self.instance))

    def test_a_closed_slot_does_not(self):
        for closed in (
            StepContributionStatuses.DECLINED,
            StepContributionStatuses.WITHDRAWN,
            StepContributionStatuses.EXPIRED,
        ):
            self.slot.status = closed
            self.slot.save(update_fields=["status"])
            self.assertFalse(
                contributions.may_contribute(self.user, self.instance), closed
            )

    def test_a_slot_under_a_standing_conflict_does_not(self):
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.user),
            proposal=self.proposal,
            call=self.fixture.call,
        )
        self.assertFalse(contributions.may_contribute(self.user, self.instance))
