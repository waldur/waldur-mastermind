"""Earlier steps' outputs read by later steps, the applicant's response to
reviews and the report released to the applicant.

The call runs: administrative check, expert review, rapporteurs' questions,
the applicant's response to both, the rapporteurs' individual reports on all
three, and a consolidated report released to the applicant.
"""

import datetime
from unittest import mock

from django.core.exceptions import ValidationError as DjangoValidationError
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_transfer,
    contributions,
    models,
    tasks,
    utils,
    views,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    CallStates,
    ContributorSources,
    NotificationRuleTriggers,
    OutcomeSets,
    ProposalRoleEligibility,
    ResponsibleRoles,
    StepContributionStatuses,
    StepKinds,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_call_steps import CallStepsTestMixin
from waldur_mastermind.proposal.tests.test_step_contributions import (
    PerOfferingAssessmentBase,
)

PUBLIC = "The method is sound."
PRIVATE = "Between us: the team is weak."


def _form(question_text="Your answer"):
    checklist = checklist_factories.ChecklistFactory(
        checklist_type=checklist_enums.ChecklistTypes.WORKFLOW_STEP
    )
    question = checklist_factories.QuestionFactory(
        checklist=checklist,
        required=True,
        description=question_text,
        question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
    )
    return checklist, question


class StepInputsTestMixin(CallStepsTestMixin):
    def setUp(self):
        super().setUp()
        self.manager = self.fixture.call_manager
        self.applicant = self.fixture.proposal_creator
        self.team_member = structure_factories.UserFactory()
        self.proposal.add_user(self.team_member, ProposalRole.MEMBER)
        self.bystander = structure_factories.UserFactory()
        self.reviewer = self.fixture.reviewer_1
        # Reviews stay private to the applicant unless released.
        self.call.reviews_visible_to_submitters = False
        self.call.reviewer_identity_visible_to_submitters = False
        self.call.save()

        models.CallWorkflowStep.objects.filter(
            call=self.call, key__in=["technical_assessment", "panel_review"]
        ).update(is_enabled=False)
        self.review_step = models.CallWorkflowStep.objects.get(
            call=self.call, key="expert_review"
        )
        self.review_step.is_enabled = True
        self.review_step.save()

        self.role = models.CallProposalRole.objects.create(
            call=self.call,
            key="rapporteur",
            name="Rapporteur",
            eligible_from=ProposalRoleEligibility.ANY_USER,
            max_holders=2,
        )
        role_source = f"proposal_role:{self.role.pk}"
        self.questions_form, self.question = _form("Questions to the applicant")
        self.questions_step = self.add_step(
            "rapporteur-questions",
            StepKinds.ASSESSMENT,
            "Rapporteur questions",
            3,
            outcome_set=OutcomeSets.DONE,
            completer=ResponsibleRoles.CALL_MANAGER,
            contributor_source=role_source,
            checklist=self.questions_form,
        )
        self.response_form, self.response_question = _form("Your response")
        self.response_step = self.add_step(
            "response-to-reviews",
            StepKinds.APPLICANT_ACTION,
            "Response to reviews",
            4,
            outcome_set=OutcomeSets.SUBMITTED,
            completer=ResponsibleRoles.APPLICANT,
            checklist=self.response_form,
            duration_in_days=7,
        )
        self.response_step.input_steps.set([self.review_step, self.questions_step])
        self.report_form, self.report_question = _form("Report")
        self.individual_step = self.add_step(
            "individual-report",
            StepKinds.ASSESSMENT,
            "Individual report",
            5,
            outcome_set=OutcomeSets.DONE,
            completer=ResponsibleRoles.CALL_MANAGER,
            contributor_source=role_source,
            checklist=self.report_form,
        )
        self.individual_step.input_steps.set(
            [self.review_step, self.questions_step, self.response_step]
        )
        self.consolidated_step = self.add_step(
            "consolidated-report",
            StepKinds.ASSESSMENT,
            "Consolidated report",
            6,
            outcome_set=OutcomeSets.DONE,
            completer=ResponsibleRoles.CALL_MANAGER,
            contributor_source=role_source,
            checklist=self.report_form,
            max_contributions=1,
            release_to_applicant=True,
        )
        self.consolidated_step.input_steps.set([self.individual_step])

        self.rapporteur_1 = structure_factories.UserFactory()
        self.rapporteur_2 = structure_factories.UserFactory()
        for user, is_lead in ((self.rapporteur_1, True), (self.rapporteur_2, False)):
            models.ProposalRoleAssignment.objects.create(
                proposal=self.proposal,
                role=self.role,
                user=user,
                is_lead=is_lead,
                coi_confirmation_required=False,
            )

    # -- driving the workflow ------------------------------------------------

    def url(self, action, proposal=None):
        return factories.ProposalFactory.get_url(proposal or self.proposal, action)

    def instance(self, call_step):
        return self.proposal.workflow_step_instances.get(call_step=call_step)

    def complete_ok(self, outcome, user=None):
        response = self.complete(outcome, user=user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def answer(self, user, call_step, question, text, contribution=None):
        self.client.force_authenticate(user)
        url = (
            self.url("submit-step-checklist-answers")
            + f"?step_uuid={call_step.uuid.hex}"
        )
        if contribution is not None:
            url += f"&contribution_uuid={contribution.uuid.hex}"
        return self.client.post(
            url,
            [{"question_uuid": str(question.uuid), "answer_data": text}],
            format="json",
        )

    def contribute(self, user, call_step, question, text):
        contribution = self.instance(call_step).contributions.get(contributor=user)
        response = self.answer(user, call_step, question, text, contribution)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.client.force_authenticate(user)
        response = self.client.post(
            self.url("submit_contribution"),
            {"contribution_uuid": contribution.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return contribution

    def reach_response_step(self):
        self.submit()
        self.complete_ok("eligible")
        self.assertEqual(self.proposal.workflow_step, "expert_review")
        review = factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.reviewer,
            state=models.Review.States.SUBMITTED,
            summary_score=7,
            summary_public_comment=PUBLIC,
            summary_private_comment=PRIVATE,
            comment_team="A capable team.",
        )
        contributions.attach_review(review)
        self.complete_ok("reviewed")
        self.assertEqual(self.proposal.workflow_step, "rapporteur-questions")
        self.contribute(
            self.rapporteur_1, self.questions_step, self.question, "How do you scale?"
        )
        self.complete_ok("done")
        self.assertEqual(self.proposal.workflow_step, "response-to-reviews")
        return review

    def respond(self, text="It scales linearly."):
        response = self.answer(
            self.applicant, self.response_step, self.response_question, text
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.complete_ok("submitted", user=self.applicant)

    def reach_consolidated_step(self):
        self.reach_response_step()
        self.respond()
        self.assertEqual(self.proposal.workflow_step, "individual-report")
        self.contribute(
            self.rapporteur_1, self.individual_step, self.report_question, "Fund it."
        )
        self.complete_ok("done")
        self.assertEqual(self.proposal.workflow_step, "consolidated-report")
        self.contribute(
            self.rapporteur_1,
            self.consolidated_step,
            self.report_question,
            "Consolidated: fund it, with a smaller allocation.",
        )
        self.complete_ok("done")

    def inputs(self, user, call_step):
        self.client.force_authenticate(user)
        return self.client.get(
            self.url("step_inputs"), {"step_uuid": call_step.uuid.hex}
        )

    def released(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(self.url("released_reports"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def release(self, user, call_step=None, text=""):
        self.client.force_authenticate(user)
        return self.client.post(
            self.url("release_step_report"),
            {"step_uuid": (call_step or self.consolidated_step).uuid.hex, "text": text},
            format="json",
        )


class ApplicantResponseTest(StepInputsTestMixin, test.APITestCase):
    def test_applicant_reads_the_public_parts_of_the_inputs(self):
        self.reach_response_step()
        for user in (self.applicant, self.team_member):
            response = self.inputs(user, self.response_step)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            review_output, questions_output = response.data
            self.assertEqual(review_output["step"]["step"], "expert_review")
            [review] = review_output["reviews"]
            self.assertEqual(review["summary_public_comment"], PUBLIC)
            self.assertEqual(review["comment_team"], "A capable team.")
            self.assertNotIn("summary_private_comment", review)
            self.assertNotIn(PRIVATE, str(response.data))
            # The call conceals reviewers from applicants.
            for field in ("reviewer", "reviewer_uuid", "reviewer_full_name"):
                self.assertNotIn(field, review)
            self.assertNotIn(self.reviewer.full_name, str(response.data))
            self.assertTrue(review["anonymous_reviewer_name"])
            # A reviewer's own form stays with the call team.
            self.assertEqual(review_output["contributions"], [])
            # The review outcome stays masked, as on the step list.
            self.assertIsNone(review_output["step"]["outcome"])
            self.assertIsNone(review_output["step"]["internal_notes"])

            [question] = questions_output["contributions"]
            self.assertEqual(question["answers"][0]["answer_data"], "How do you scale?")
            self.assertIsNone(question["contributor_uuid"])
            self.assertNotIn(self.rapporteur_1.full_name, str(response.data))

    def test_reviewer_identity_follows_the_call(self):
        self.call.reviewer_identity_visible_to_submitters = True
        self.call.save()
        self.reach_response_step()
        [review] = self.inputs(self.team_member, self.response_step).data[0]["reviews"]
        self.assertEqual(review["reviewer_uuid"], self.reviewer.uuid.hex)
        self.assertNotIn("summary_private_comment", review)

    def test_applicant_reads_nothing_before_the_step_starts(self):
        self.submit()
        self.complete_ok("eligible")
        response = self.inputs(self.applicant, self.response_step)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_applicant_does_not_read_the_inputs_of_an_evaluator_step(self):
        self.reach_response_step()
        self.respond()
        for user in (self.applicant, self.team_member):
            response = self.inputs(user, self.individual_step)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_response_is_read_only_once_submitted(self):
        self.reach_response_step()
        self.respond()
        instance = self.instance(self.response_step)
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(instance.outcome, WorkflowStepOutcomes.SUBMITTED)
        response = self.answer(
            self.applicant, self.response_step, self.response_question, "Changed"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # It stays readable to the applicant after the step.
        response = self.inputs(self.applicant, self.response_step)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_no_response_cannot_be_chosen(self):
        self.reach_response_step()
        response = self.complete("no_response", user=self.applicant)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_lapsed_response_step_advances_as_no_response(self):
        self.reach_response_step()
        # Answered, but never submitted.
        self.answer(self.applicant, self.response_step, self.response_question, "Hm")
        instance = self.instance(self.response_step)
        instance.deadline = timezone.now() - datetime.timedelta(minutes=1)
        instance.save()

        tasks.mark_expired_workflow_steps()

        instance.refresh_from_db()
        self.proposal.refresh_from_db()
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.COMPLETED)
        self.assertEqual(instance.outcome, WorkflowStepOutcomes.NO_RESPONSE)
        self.assertEqual(self.proposal.workflow_step, "individual-report")
        self.assertEqual(self.proposal.state, "in_review")
        # The unsubmitted answer is no output: the rapporteurs read none.
        response = self.inputs(self.rapporteur_1, self.individual_step)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[2]["contributions"], [])
        self.assertEqual(response.data[2]["step"]["outcome"], "no_response")


class EvaluatorInputsTest(StepInputsTestMixin, test.APITestCase):
    def test_rapporteurs_read_the_reviews_questions_and_response(self):
        self.reach_response_step()
        self.respond("It scales linearly.")
        response = self.inputs(self.rapporteur_2, self.individual_step)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        review_output, questions_output, response_output = response.data
        [review] = review_output["reviews"]
        self.assertEqual(review["summary_public_comment"], PUBLIC)
        self.assertNotIn("summary_private_comment", review)
        # The reviewer's contribution is their review, not listed twice.
        self.assertEqual(review_output["contributions"], [])
        [question] = questions_output["contributions"]
        self.assertEqual(question["answers"][0]["answer_data"], "How do you scale?")
        [answer] = response_output["contributions"]
        self.assertEqual(answer["answers"][0]["answer_data"], "It scales linearly.")
        self.assertEqual(response_output["step"]["outcome"], "submitted")

        # And open it, its files included, as a contribution of their own.
        contribution = models.StepContribution.objects.get(uuid=answer["uuid"])
        self.assertTrue(contributions.user_can_read(self.rapporteur_2, contribution))
        self.assertFalse(contributions.user_can_read(self.bystander, contribution))
        self.assertFalse(contributions.user_can_read(self.reviewer, contribution))

    def test_applicant_identity_is_concealed_from_rapporteurs(self):
        models.CallApplicantVisibilityConfig.objects.update_or_create(
            call=self.call,
            defaults={
                "expose_full_name": False,
                "expose_username": False,
                "expose_email": False,
            },
        )
        self.reach_response_step()
        self.respond()
        response = self.inputs(self.rapporteur_1, self.individual_step)
        answer = response.data[2]["contributions"][0]
        self.assertIsNone(answer["contributor_uuid"])
        self.assertNotIn(self.applicant.full_name, str(response.data))

    def test_users_without_work_on_the_step_read_nothing(self):
        self.reach_response_step()
        self.respond()
        for user in (self.bystander, self.reviewer):
            response = self.inputs(user, self.individual_step)
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )
        # A revoked rapporteur loses it.
        models.ProposalRoleAssignment.objects.filter(user=self.rapporteur_2).update(
            revoked_at=timezone.now()
        )
        response = self.inputs(self.rapporteur_2, self.individual_step)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_call_manager_reads_every_input(self):
        self.reach_response_step()
        response = self.inputs(self.manager, self.individual_step)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [review] = response.data[0]["reviews"]
        self.assertEqual(review["summary_private_comment"], PRIVATE)

    def test_workflow_states_name_the_inputs(self):
        self.submit()
        self.client.force_authenticate(self.manager)
        states = {
            s["step"]: s for s in self.client.get(self.url("workflow_states")).data
        }
        self.assertEqual(
            states["response-to-reviews"]["input_step_uuids"],
            [self.review_step.uuid.hex, self.questions_step.uuid.hex],
        )
        self.assertEqual(states["administrative_check"]["input_step_uuids"], [])
        self.assertTrue(states["consolidated-report"]["release_to_applicant"])
        self.assertIsNone(states["consolidated-report"]["released_at"])


class BlindReviewInputsTest(StepInputsTestMixin, test.APITestCase):
    """A blind review read as the input of a later step keeps its authors
    from the evaluators reading it; the call team still knows them."""

    IDENTITY_FIELDS = ("reviewer", "reviewer_full_name", "reviewer_uuid")

    def setUp(self):
        super().setUp()
        for call_step in (self.review_step, self.questions_step):
            call_step.blind_review = True
            call_step.save(update_fields=["blind_review"])
        self.reach_response_step()
        self.respond()

    def label_of(self, user, call_step):
        """The label ``user`` goes by on ``call_step``: their number among
        its contributors by uuid."""
        slots = sorted(
            self.instance(call_step).contributions.filter(review__isnull=True),
            key=lambda slot: slot.uuid.hex,
        )
        index = [slot.contributor_id for slot in slots].index(user.pk)
        return f"Contributor {index + 1}"

    def test_labels_number_each_step_in_an_order_carrying_nothing(self):
        questions = self.instance(self.questions_step)
        individual = self.instance(self.individual_step)
        # The lead's slot was made first on both steps; uuids put it last on
        # one and first on the other.
        for instance, first, second, prefix in (
            (questions, self.rapporteur_2, self.rapporteur_1, "1"),
            (individual, self.rapporteur_1, self.rapporteur_2, "2"),
        ):
            for user, end in ((first, "0"), (second, "f")):
                instance.contributions.filter(contributor=user).update(
                    uuid=prefix + end * 31
                )
        _, labels = contributions.anonymous_labels(self.proposal)

        def label(instance, user):
            return str(labels[instance.contributions.get(contributor=user).pk])

        self.assertEqual(label(questions, self.rapporteur_2), "Contributor 1")
        self.assertEqual(label(questions, self.rapporteur_1), "Contributor 2")
        self.assertEqual(label(individual, self.rapporteur_1), "Contributor 1")
        self.assertEqual(label(individual, self.rapporteur_2), "Contributor 2")

        response = self.inputs(self.rapporteur_2, self.individual_step)
        [question] = response.data[1]["contributions"]
        self.assertEqual(question["contributor_full_name"], "Contributor 2")

    def test_evaluators_read_the_blind_inputs_anonymised(self):
        response = self.inputs(self.rapporteur_2, self.individual_step)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        review_output, questions_output, _response_output = response.data
        [review] = review_output["reviews"]
        for field in (*self.IDENTITY_FIELDS, "reviewer_image"):
            self.assertNotIn(field, review)
        self.assertEqual(review["anonymous_reviewer_name"], "Reviewer 1")
        self.assertEqual(review["summary_public_comment"], PUBLIC)
        [question] = questions_output["contributions"]
        self.assertIsNone(question["contributor_uuid"])
        self.assertIsNone(question["user_uuid"])
        self.assertIsNone(question["user_image"])
        label = self.label_of(self.rapporteur_1, self.questions_step)
        self.assertEqual(question["contributor_full_name"], label)
        self.assertEqual(question["user_full_name"], label)
        for user in (self.reviewer, self.rapporteur_1):
            self.assertNotIn(user.full_name, str(response.data))
            self.assertNotIn(user.uuid.hex, str(response.data))
            self.assertNotIn(user.email, str(response.data))

        # The same contribution listed on its own says no more; the
        # reader's own slot is theirs to know.
        self.client.force_authenticate(self.rapporteur_2)
        listed = {
            row["uuid"]: row
            for row in self.client.get(
                self.url("contributions"),
                {"step_uuid": self.questions_step.uuid.hex},
            ).data
        }
        row = listed[question["uuid"]]
        self.assertEqual(row["contributor_full_name"], label)
        self.assertIsNone(row["contributor_uuid"])
        self.assertNotIn(self.rapporteur_1.full_name, str(listed))

    def test_the_completer_of_a_blind_step_is_kept_from_its_readers(self):
        # A step its contributions complete is completed by whoever
        # submitted the last of them.
        models.ProposalWorkflowStepInstance.objects.filter(
            proposal=self.proposal, call_step=self.questions_step
        ).update(completed_by=self.rapporteur_1)
        response = self.inputs(self.rapporteur_2, self.individual_step)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.data[1]["step"]["completed_by"])
        self.assertNotIn(self.rapporteur_1.uuid.hex, str(response.data))
        self.client.force_authenticate(self.rapporteur_2)
        states = {
            s["step"]: s for s in self.client.get(self.url("workflow_states")).data
        }
        self.assertIsNone(states["rapporteur-questions"]["completed_by"])

        # The call manager still knows.
        self.client.force_authenticate(self.manager)
        states = {
            s["step"]: s for s in self.client.get(self.url("workflow_states")).data
        }
        completed_by = states["rapporteur-questions"]["completed_by"]
        self.assertEqual(str(completed_by).replace("-", ""), self.rapporteur_1.uuid.hex)

    def test_labels_are_stable_across_readings(self):
        first = self.inputs(self.rapporteur_2, self.individual_step).data
        second = self.inputs(self.rapporteur_2, self.individual_step).data
        self.assertEqual(
            first[0]["reviews"][0]["anonymous_reviewer_name"],
            second[0]["reviews"][0]["anonymous_reviewer_name"],
        )
        self.assertEqual(
            first[1]["contributions"][0]["user_full_name"],
            second[1]["contributions"][0]["user_full_name"],
        )

    def test_the_call_manager_and_staff_see_who_wrote_them(self):
        for user in (self.manager, self.fixture.staff):
            response = self.inputs(user, self.individual_step)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            [review] = response.data[0]["reviews"]
            self.assertEqual(review["reviewer_full_name"], self.reviewer.full_name)
            self.assertNotIn("anonymous_reviewer_name", review)
            [question] = response.data[1]["contributions"]
            self.assertEqual(question["contributor_uuid"], self.rapporteur_1.uuid.hex)
            self.assertEqual(
                question["contributor_full_name"], self.rapporteur_1.full_name
            )

    def test_an_open_review_still_names_its_authors(self):
        for call_step in (self.review_step, self.questions_step):
            call_step.blind_review = False
            call_step.save(update_fields=["blind_review"])
        response = self.inputs(self.rapporteur_2, self.individual_step)
        [review] = response.data[0]["reviews"]
        self.assertEqual(review["reviewer_full_name"], self.reviewer.full_name)
        [question] = response.data[1]["contributions"]
        self.assertEqual(question["contributor_full_name"], self.rapporteur_1.full_name)


class ReleaseTest(StepInputsTestMixin, test.APITestCase):
    def test_applicant_reads_the_report_only_once_released(self):
        self.reach_consolidated_step()
        self.assertFalse(self.call.reviews_visible_to_submitters)
        self.assertEqual(self.released(self.applicant), [])

        response = self.release(self.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNotNone(response.data["released_at"])
        instance = self.instance(self.consolidated_step)
        self.assertEqual(instance.released_by, self.manager)

        for user in (self.applicant, self.team_member):
            [report] = self.released(user)
            self.assertEqual(report["step_name"], "Consolidated report")
            self.assertIsNone(report["released_by_name"])
            [contribution] = report["contributions"]
            self.assertIn("smaller allocation", str(contribution["answers"]))
            self.assertIsNone(contribution["contributor_uuid"])
            self.assertTrue(
                contributions.user_can_read(
                    user, models.StepContribution.objects.get(uuid=contribution["uuid"])
                )
            )
        # The call team sees who released it, for the allocation decision.
        [report] = self.released(self.manager)
        self.assertEqual(report["released_by_name"], self.manager.full_name)
        # An evaluator who is neither reads none.
        self.assertEqual(self.released(self.rapporteur_2), [])

    def test_edited_text_replaces_the_report_for_the_applicant(self):
        self.reach_consolidated_step()
        response = self.release(self.manager, text="Edited feedback.")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        [report] = self.released(self.applicant)
        self.assertEqual(report["text"], "Edited feedback.")
        self.assertEqual(report["contributions"], [])
        self.assertNotIn("smaller allocation", str(report))
        contribution = self.instance(self.consolidated_step).contributions.get(
            contributor=self.rapporteur_1
        )
        self.assertFalse(contributions.user_can_read(self.applicant, contribution))
        [report] = self.released(self.manager)
        self.assertEqual(len(report["contributions"]), 1)

    def test_release_is_refused_before_completion_twice_and_elsewhere(self):
        self.reach_response_step()
        self.respond()
        response = self.release(self.manager)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.release(self.manager, call_step=self.individual_step)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_release_is_the_completers_or_the_call_managers(self):
        self.reach_consolidated_step()
        for user in (self.applicant, self.rapporteur_2, self.bystander):
            response = self.release(user)
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )
        self.assertEqual(self.release(self.manager).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.release(self.manager).status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_a_held_decision_holds_the_report_back(self):
        self.reach_consolidated_step()
        self.release(self.manager)
        self.proposal.decision_held = True
        self.proposal.save(update_fields=["decision_held"])
        self.assertEqual(self.released(self.applicant), [])
        self.assertEqual(len(self.released(self.manager)), 1)


class InputStepConfigTest(StepInputsTestMixin, test.APITestCase):
    def step_url(self, call_step):
        return (
            f"http://testserver/api/proposal-protected-calls/{self.call.uuid.hex}"
            f"/workflow_steps/{call_step.uuid.hex}/"
        )

    def patch(self, call_step, data):
        self.client.force_authenticate(self.manager)
        return self.client.patch(self.step_url(call_step), data, format="json")

    def test_only_earlier_steps_of_the_call_are_inputs(self):
        response = self.patch(
            self.questions_step, {"input_steps": [self.review_step.uuid.hex]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["input_steps_names"], ["Expert review"])

        response = self.patch(
            self.questions_step, {"input_steps": [self.individual_step.uuid.hex]}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("input_steps", response.data)

        response = self.patch(
            self.questions_step, {"input_steps": [self.questions_step.uuid.hex]}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        other = factories.CallFactory()
        foreign = models.CallWorkflowStep.objects.filter(call=other).first()
        response = self.patch(self.questions_step, {"input_steps": [foreign.uuid.hex]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_an_assessment_releases_its_report(self):
        response = self.patch(self.response_step, {"release_to_applicant": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_single_lead_report_is_released_by_default(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            f"http://testserver/api/proposal-protected-calls/{self.call.uuid.hex}"
            "/workflow_steps/",
            {
                "kind": StepKinds.ASSESSMENT,
                "name": "Final feedback",
                "contributor_source": f"proposal_role:{self.role.pk}",
                "max_contributions": 1,
                "completer": ResponsibleRoles.CALL_MANAGER,
                "outcome_set": OutcomeSets.DONE,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(response.data["release_to_applicant"])

    def test_duplicate_copies_the_inputs(self):
        copy = utils.duplicate_call(self.call, "Copy", self.manager)
        response_step = copy.workflow_steps.get(key="response-to-reviews")
        self.assertEqual(
            sorted(s.key for s in response_step.input_steps.all()),
            ["expert_review", "rapporteur-questions"],
        )
        self.assertTrue(
            copy.workflow_steps.get(key="consolidated-report").release_to_applicant
        )

    def test_export_and_import_carry_the_inputs(self):
        data, _warnings = call_transfer.export_call(self.call)
        exported = {s["key"]: s for s in data["workflow_steps"]}
        self.assertEqual(
            exported["individual-report"]["input_steps"],
            ["expert_review", "rapporteur-questions", "response-to-reviews"],
        )
        call, _sections, _warnings = call_transfer.import_call(
            data, self.call.manager, self.manager, name="Imported"
        )
        individual = call.workflow_steps.get(key="individual-report")
        self.assertEqual(
            [s.key for s in individual.input_steps.order_by("order")],
            ["expert_review", "rapporteur-questions", "response-to-reviews"],
        )
        self.assertTrue(
            call.workflow_steps.get(key="consolidated-report").release_to_applicant
        )


class CallWithoutInputsTest(CallStepsTestMixin, test.APITestCase):
    def test_a_lapsed_applicant_step_without_inputs_expires_as_before(self):
        step = self.add_step(
            "statement",
            StepKinds.APPLICANT_ACTION,
            "Statement",
            1,
            outcome_set=OutcomeSets.SUBMITTED,
            completer=ResponsibleRoles.APPLICANT,
        )
        self.submit()
        self.complete("eligible")
        instance = self.proposal.workflow_step_instances.get(call_step=step)
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.ACTIVE)
        instance.deadline = timezone.now() - datetime.timedelta(minutes=1)
        instance.save()
        tasks.mark_expired_workflow_steps()
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.EXPIRED)
        self.assertEqual(instance.outcome, WorkflowStepOutcomes.EXPIRED)


PRIVATE_2 = "Second reviewer, privately: reject."


class FormerHolderTest(StepInputsTestMixin, test.APITestCase):
    """Who no longer holds the step's work reads nothing of its inputs, even
    with a contribution left over from before."""

    def setUp(self):
        super().setUp()
        self.reach_response_step()
        self.respond("The response")
        self.response_contribution = self.instance(
            self.response_step
        ).contributions.get()
        # The second rapporteur has a slot on the running individual report.
        self.assertTrue(
            self.instance(self.individual_step)
            .contributions.filter(contributor=self.rapporteur_2)
            .exists()
        )

    def assert_reads_nothing(self, user):
        self.assertFalse(contributions.user_can_read(user, self.response_contribution))
        self.assertFalse(self.response_contribution.user_can_read_checklist(user))
        response = self.inputs(user, self.individual_step)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertNotIn("The response", str(getattr(response, "data", "")))

    def test_a_revoked_rapporteur(self):
        self.assertTrue(
            contributions.user_can_read(self.rapporteur_2, self.response_contribution)
        )
        models.ProposalRoleAssignment.objects.filter(user=self.rapporteur_2).update(
            revoked_at=timezone.now()
        )
        self.assert_reads_nothing(self.rapporteur_2)

    def test_a_rapporteur_with_a_standing_conflict(self):
        profile = models.ReviewerProfile.objects.create(user=self.rapporteur_2)
        models.ConflictOfInterest.objects.create(
            reviewer=profile,
            proposal=self.proposal,
            call=self.call,
            coi_type="INST_SAME",
            status="pending",
            evidence_description="Same institute",
        )
        self.assert_reads_nothing(self.rapporteur_2)


class RevokedReviewingRapporteurTest(StepInputsTestMixin, test.APITestCase):
    def test_a_reviewer_once_a_rapporteur_reads_nothing(self):
        models.ProposalRoleAssignment.objects.create(
            proposal=self.proposal,
            role=self.role,
            user=self.reviewer,
            coi_confirmation_required=False,
        )
        self.reach_response_step()
        self.respond("The response")
        models.ProposalRoleAssignment.objects.filter(user=self.reviewer).update(
            revoked_at=timezone.now()
        )
        response = self.inputs(self.reviewer, self.individual_step)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertNotIn("The response", str(getattr(response, "data", "")))


class ApplicantConcealmentTest(StepInputsTestMixin, test.APITestCase):
    def test_every_path_conceals_the_applicant_from_rapporteurs(self):
        models.CallApplicantVisibilityConfig.objects.update_or_create(
            call=self.call,
            defaults={
                "expose_full_name": False,
                "expose_username": False,
                "expose_email": False,
            },
        )
        self.reach_response_step()
        self.respond()
        self.client.force_authenticate(self.rapporteur_1)
        for action in ("contributions", "step-checklist-responses"):
            response = self.client.get(
                self.url(action), {"step_uuid": self.response_step.uuid.hex}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, action)
            self.assertEqual(len(response.data), 1, action)
            self.assertNotIn(self.applicant.full_name, str(response.data), action)
            self.assertNotIn(self.applicant.uuid.hex, str(response.data), action)
        response = self.inputs(self.rapporteur_1, self.individual_step)
        self.assertNotIn(self.applicant.full_name, str(response.data))
        self.assertNotIn(self.applicant.uuid.hex, str(response.data))
        # The call manager is not concealed from.
        self.client.force_authenticate(self.manager)
        response = self.client.get(
            self.url("contributions"), {"step_uuid": self.response_step.uuid.hex}
        )
        self.assertIn(self.applicant.full_name, str(response.data))


class ReviewMaskingPerReviewTest(StepInputsTestMixin, test.APITestCase):
    def test_a_rapporteur_who_reviewed_reads_only_their_own_private_comment(self):
        models.ProposalRoleAssignment.objects.create(
            proposal=self.proposal,
            role=self.role,
            user=self.reviewer,
            coi_confirmation_required=False,
        )
        self.submit()
        self.complete_ok("eligible")
        for reviewer, comment in (
            (self.reviewer, PRIVATE),
            (self.fixture.reviewer_2, PRIVATE_2),
        ):
            review = factories.ReviewFactory(
                proposal=self.proposal,
                reviewer=reviewer,
                state=models.Review.States.SUBMITTED,
                summary_private_comment=comment,
            )
            contributions.attach_review(review)
        self.complete_ok("reviewed")
        self.contribute(self.rapporteur_1, self.questions_step, self.question, "Q?")
        self.complete_ok("done")
        self.respond()
        response = self.inputs(self.reviewer, self.individual_step)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn(PRIVATE, str(response.data))
        self.assertNotIn(PRIVATE_2, str(response.data))
        # The applicant reads one number per reviewer, in order.
        response = self.inputs(self.applicant, self.response_step)
        names = [r["anonymous_reviewer_name"] for r in response.data[0]["reviews"]]
        self.assertEqual(names, ["Reviewer 1", "Reviewer 2"])


class TeamReadsResponseTest(StepInputsTestMixin, test.APITestCase):
    def test_the_team_reads_the_submitted_response(self):
        self.reach_response_step()
        self.respond("Our response")
        contribution = self.instance(self.response_step).contributions.get()
        self.assertEqual(contribution.status, "submitted")
        self.assertTrue(contributions.user_can_read(self.team_member, contribution))
        self.client.force_authenticate(self.team_member)
        for action in ("contributions", "step-checklist-responses"):
            response = self.client.get(
                self.url(action), {"step_uuid": self.response_step.uuid.hex}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, action)
            self.assertEqual(len(response.data), 1, action)
        self.assertIn("Our response", str(response.data))

    def test_a_hand_withdrawn_slot_is_no_output(self):
        self.reach_response_step()
        self.respond("Taken back")
        self.instance(self.response_step).contributions.update(status="withdrawn")
        response = self.inputs(self.rapporteur_1, self.individual_step)
        self.assertEqual(response.data[2]["contributions"], [])
        self.assertNotIn("Taken back", str(response.data))


class PerOfferingInputsTest(PerOfferingAssessmentBase):
    """A later per-offering step reads the technical assessment: each team
    reads its own offering's slot, not the others'."""

    def setUp(self):
        super().setUp()
        models.CallWorkflowStep.make_room(self.call.pk, self.call_step.order + 1)
        self.later = models.CallWorkflowStep.objects.create(
            call=self.call,
            key="followup",
            kind=StepKinds.ASSESSMENT,
            name="Follow-up",
            order=self.call_step.order + 1,
            contributor_source=ContributorSources.REQUESTED_OFFERING_TEAM,
            per_requested_offering=True,
            outcome_set=OutcomeSets.PER_OFFERING,
            checklist=self.checklist,
        )
        self.later.input_steps.set([self.call_step])
        self.proposal.current_step = None
        self.proposal.save(update_fields=["current_step"])
        self.proposal.workflow_step_instances.all().delete()
        workflow_service.create_step_instances(self.proposal)
        workflow_service.activate_first_step(self.proposal)
        self.proposal.refresh_from_db()
        self.instance = self.proposal.current_step

    def test_a_team_reads_only_its_own_offering(self):
        slot_a = self._slot(self.requested_a)
        slot_b = self._slot(self.requested_b)
        self._answer(self.manager_b, slot_b, "B's own conclusion")
        self._submit(self.manager_b, slot_b, "accepted")
        self._answer(self.manager_a, slot_a, "A's own conclusion")
        self._submit(self.manager_a, slot_a, "accepted")
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.workflow_step, "followup")
        slot_b.refresh_from_db()
        self.assertFalse(contributions.user_can_read(self.manager_a, slot_b))
        self.client.force_authenticate(self.manager_a)
        response = self.client.get(
            self._url("step_inputs"), {"step_uuid": self.later.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("A's own conclusion", str(response.data))
        self.assertNotIn("B's own conclusion", str(response.data))
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(
            self._url("step_inputs"), {"step_uuid": self.later.uuid.hex}
        )
        self.assertIn("B's own conclusion", str(response.data))


class RapporteurReadsPerOfferingInputsTest(PerOfferingAssessmentBase):
    """Rapporteurs reporting on the proposal read every partition's
    technical assessment; each provider still reads only its own."""

    def setUp(self):
        super().setUp()
        self.role = models.CallProposalRole.objects.create(
            call=self.call,
            key="rapporteur",
            name="Rapporteur",
            eligible_from=ProposalRoleEligibility.ANY_USER,
            max_holders=1,
        )
        models.CallWorkflowStep.make_room(self.call.pk, self.call_step.order + 1)
        self.report_step = models.CallWorkflowStep.objects.create(
            call=self.call,
            key="report",
            kind=StepKinds.ASSESSMENT,
            name="Report",
            order=self.call_step.order + 1,
            contributor_source=f"proposal_role:{self.role.pk}",
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.role,
            outcome_set=OutcomeSets.DONE,
            checklist=self.checklist,
        )
        self.report_step.input_steps.set([self.call_step])
        self.rapporteur = structure_factories.UserFactory()
        models.ProposalRoleAssignment.objects.create(
            proposal=self.proposal,
            role=self.role,
            user=self.rapporteur,
            is_lead=True,
            coi_confirmation_required=False,
        )
        self.proposal.current_step = None
        self.proposal.save(update_fields=["current_step"])
        self.proposal.workflow_step_instances.all().delete()
        workflow_service.create_step_instances(self.proposal)
        workflow_service.activate_first_step(self.proposal)
        self.proposal.refresh_from_db()
        self.instance = self.proposal.current_step

    def inputs(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            self._url("step_inputs"), {"step_uuid": self.report_step.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return str(response.data)

    def test_rapporteur_reads_every_partition_and_providers_their_own(self):
        slot_a = self._slot(self.requested_a)
        slot_b = self._slot(self.requested_b)
        self._answer(self.manager_a, slot_a, "A's own conclusion")
        self._submit(self.manager_a, slot_a, "accepted", "Only on GPUs")
        self._answer(self.manager_b, slot_b, "B's own conclusion")
        self._submit(self.manager_b, slot_b, "rejected")
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.workflow_step, "report")
        slot_a.refresh_from_db()
        slot_b.refresh_from_db()

        data = self.inputs(self.rapporteur)
        for text in ("A's own conclusion", "B's own conclusion", "Only on GPUs"):
            self.assertIn(text, data)
        for slot in (slot_a, slot_b):
            self.assertTrue(contributions.user_can_read(self.rapporteur, slot))
        # Provider isolation holds.
        self.assertFalse(contributions.user_can_read(self.manager_a, slot_b))
        self.assertFalse(contributions.user_can_read(self.manager_b, slot_a))


class ApplicantStepFormAtActivationTest(StepInputsTestMixin, test.APITestCase):
    """An applicant's submission step is answered through its form; a call
    is not activated with one the applicant could only submit empty."""

    def activate(self):
        self.call.state = CallStates.DRAFT
        self.call.save(update_fields=["state"])
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(
            factories.CallFactory.get_protected_url(self.call, "activate")
        )

    def test_a_response_step_without_a_form_stops_activation(self):
        self.response_step.checklist = None
        self.response_step.save(update_fields=["checklist"])
        response = self.activate()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("have no form", str(response.data))
        self.assertIn("Response to reviews", str(response.data))

    def test_a_disabled_one_does_not(self):
        self.response_step.checklist = None
        self.response_step.is_enabled = False
        self.response_step.save(update_fields=["checklist", "is_enabled"])
        self.assertNotIn("have no form", str(self.activate().data))

    def test_a_response_step_with_its_form_passes_the_check(self):
        self.assertNotIn("have no form", str(self.activate().data))


class ResponseStepNeedsDecisionTest(StepInputsTestMixin, test.APITestCase):
    def calls_url(self, suffix=""):
        return (
            f"http://testserver/api/proposal-protected-calls/{self.call.uuid.hex}"
            f"/workflow_steps/{suffix}"
        )

    def test_a_response_step_cannot_be_moved_past_every_decision(self):
        allocation = models.CallWorkflowStep.objects.get(
            call=self.call, is_allocation_decision=True
        )
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            self.calls_url(f"{self.response_step.uuid.hex}/"),
            {"order": allocation.order + 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_response_step_cannot_be_added_at_the_end(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            self.calls_url(),
            {
                "kind": StepKinds.APPLICANT_ACTION,
                "name": "Late response",
                "outcome_set": OutcomeSets.SUBMITTED,
                "input_steps": [self.review_step.uuid.hex],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Without inputs it is a plain applicant step, as before.
        response = self.client.post(
            self.calls_url(),
            {
                "kind": StepKinds.APPLICANT_ACTION,
                "name": "Statement",
                "outcome_set": OutcomeSets.SUBMITTED,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_the_decision_after_it_cannot_go(self):
        allocation = models.CallWorkflowStep.objects.get(
            call=self.call, is_allocation_decision=True
        )
        self.assertEqual(
            models.CallWorkflowStep.undecided_response_steps(
                self.call.pk, removed_pk=allocation.pk
            ),
            ["Response to reviews"],
        )
        self.assertEqual(
            models.CallWorkflowStep.undecided_response_steps(self.call.pk), []
        )

    def test_an_input_step_cannot_move_after_its_reader(self):
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            self.calls_url(f"{self.questions_step.uuid.hex}/"),
            {"order": self.individual_step.order + 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("order", response.data)
        self.assertTrue(
            self.response_step.input_steps.filter(pk=self.questions_step.pk).exists()
        )


class InputsFixedAtSubmissionTest(StepInputsTestMixin, test.APITestCase):
    def test_changing_the_inputs_leaves_submitted_proposals_alone(self):
        self.reach_response_step()
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            f"http://testserver/api/proposal-protected-calls/{self.call.uuid.hex}"
            f"/workflow_steps/{self.response_step.uuid.hex}/",
            {"input_steps": []},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self.inputs(self.applicant, self.response_step)
        self.assertEqual(len(response.data), 2)
        instance = self.instance(self.response_step)
        instance.deadline = timezone.now() - datetime.timedelta(minutes=1)
        instance.save()
        tasks.mark_expired_workflow_steps()
        instance.refresh_from_db()
        self.assertEqual(instance.outcome, WorkflowStepOutcomes.NO_RESPONSE)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.workflow_step, "individual-report")


class NoResponseNotificationTest(StepInputsTestMixin, test.APITestCase):
    def test_a_lapsed_response_is_reported_completed_not_expired(self):
        self.reach_response_step()
        instance = self.instance(self.response_step)
        instance.deadline = timezone.now() - datetime.timedelta(minutes=1)
        instance.save()
        with mock.patch(
            "waldur_mastermind.proposal.workflow_service.notification_rules"
            ".dispatch_step_event"
        ) as dispatch:
            tasks.mark_expired_workflow_steps()
        triggers = [
            call.args[1] for call in dispatch.call_args_list if call.args[0] == instance
        ]
        self.assertEqual(triggers, [NotificationRuleTriggers.STEP_COMPLETED])


class ReleaseRecordTest(StepInputsTestMixin, test.APITestCase):
    def test_the_release_is_logged(self):
        self.reach_consolidated_step()
        self.release(self.manager)
        event = Event.objects.get(event_type=EventType.PROPOSAL_REPORT_RELEASED)
        self.assertIn("Consolidated report", event.message)

    def released_events(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            "http://testserver" + reverse("event-list"),
            {
                "scope": factories.ProposalFactory.get_url(self.proposal),
                "event_type": EventType.PROPOSAL_REPORT_RELEASED,
            },
        )
        if response.status_code == status.HTTP_400_BAD_REQUEST:
            # Who cannot read the proposal cannot name it as a scope.
            return []
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_only_those_overseeing_the_call_read_who_released(self):
        self.consolidated_step.completer = ResponsibleRoles.PROPOSAL_ROLE
        self.consolidated_step.completer_proposal_role = self.role
        self.consolidated_step.save()
        self.reach_response_step()
        self.respond()
        self.contribute(
            self.rapporteur_1, self.individual_step, self.report_question, "Fund it."
        )
        self.complete_ok("done")
        self.contribute(
            self.rapporteur_1, self.consolidated_step, self.report_question, "Fund."
        )
        self.complete_ok("done", user=self.rapporteur_1)
        self.assertEqual(
            self.release(self.rapporteur_1).status_code, status.HTTP_200_OK
        )
        for user in (
            self.manager,
            self.fixture.call_organizer_user,
            self.fixture.staff,
        ):
            [event] = self.released_events(user)
            self.assertIn(self.rapporteur_1.full_name, event["message"])
        # The applicant team reads the report, not who released it -- nor
        # does anyone else, the rapporteurs and reviewers included.
        self.assertEqual(len(self.released(self.applicant)), 1)
        for user in (
            self.applicant,
            self.team_member,
            self.rapporteur_2,
            self.reviewer,
            self.bystander,
        ):
            self.assertEqual(self.released_events(user), [])
        # Managing the call does not lift it for the applicant.
        self.call.add_user(self.applicant, CallRole.MANAGER)
        self.assertEqual(self.released_events(self.applicant), [])

    def test_switching_the_release_off_takes_the_report_back(self):
        self.reach_consolidated_step()
        self.release(self.manager)
        self.assertEqual(len(self.released(self.applicant)), 1)
        self.consolidated_step.release_to_applicant = False
        self.consolidated_step.save()
        self.assertEqual(self.released(self.applicant), [])
        contribution = self.instance(self.consolidated_step).contributions.get(
            contributor=self.rapporteur_1
        )
        self.assertFalse(contributions.user_can_read(self.applicant, contribution))

    def test_the_completer_may_release(self):
        self.consolidated_step.completer = ResponsibleRoles.PROPOSAL_ROLE
        self.consolidated_step.completer_proposal_role = self.role
        self.consolidated_step.save()
        self.reach_response_step()
        self.respond()
        self.contribute(
            self.rapporteur_1, self.individual_step, self.report_question, "Fund it."
        )
        self.complete_ok("done")
        self.contribute(
            self.rapporteur_1, self.consolidated_step, self.report_question, "Fund."
        )
        self.complete_ok("done", user=self.rapporteur_1)
        self.client.force_authenticate(self.rapporteur_1)
        states = {
            s["step"]: s for s in self.client.get(self.url("workflow_states")).data
        }
        self.assertTrue(states["consolidated-report"]["can_release"])
        self.assertEqual(
            self.release(self.rapporteur_1).status_code, status.HTTP_200_OK
        )


class BlindRapporteurReportsTest(StepInputsTestMixin, test.APITestCase):
    """On a blind step that its contributing rapporteurs also complete, each
    reads their own report until the step is over, the completer included."""

    def setUp(self):
        super().setUp()
        self.individual_step.blind_review = True
        self.individual_step.completer = ResponsibleRoles.PROPOSAL_ROLE
        self.individual_step.completer_proposal_role = self.role
        self.individual_step.save()

    def listed(self, user, action):
        self.client.force_authenticate(user)
        response = self.client.get(
            self.url(action), {"step_uuid": self.individual_step.uuid.hex}
        )
        if response.status_code != status.HTTP_200_OK:
            return []
        return [row["uuid"] for row in response.data]

    def assert_reads_only_own(self, user, other):
        self.assertFalse(contributions.user_can_read(user, other))
        for action in ("contributions", "step-checklist-responses"):
            self.assertNotIn(other.uuid.hex, self.listed(user, action), action)

    def test_rapporteurs_read_only_their_own_report_until_the_step_is_over(self):
        self.reach_response_step()
        self.respond()
        self.assertEqual(self.proposal.workflow_step, "individual-report")
        second = self.contribute(
            self.rapporteur_2, self.individual_step, self.report_question, "No."
        )
        # The lead completes the step, and contributes to it too.
        self.assert_reads_only_own(self.rapporteur_1, second)
        first = self.contribute(
            self.rapporteur_1, self.individual_step, self.report_question, "Yes."
        )
        # Submitting their own does not open the other's.
        self.assert_reads_only_own(self.rapporteur_1, second)
        self.assert_reads_only_own(self.rapporteur_2, first)
        self.assertIn(first.uuid.hex, self.listed(self.rapporteur_1, "contributions"))
        # The call manager oversees the step.
        self.assertTrue(contributions.user_can_read(self.manager, second))

        self.complete_ok("done", user=self.rapporteur_1)
        self.assertEqual(self.proposal.workflow_step, "consolidated-report")
        # Over, the reports are the consolidated report's inputs.
        response = self.inputs(self.rapporteur_1, self.consolidated_step)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        [individual] = response.data
        self.assertEqual(
            {c["uuid"] for c in individual["contributions"]},
            {first.uuid.hex, second.uuid.hex},
        )

    def test_outside_a_blind_step_the_rapporteurs_read_each_other(self):
        self.individual_step.blind_review = False
        self.individual_step.save()
        self.reach_response_step()
        self.respond()
        second = self.contribute(
            self.rapporteur_2, self.individual_step, self.report_question, "No."
        )
        self.assertTrue(contributions.user_can_read(self.rapporteur_1, second))
        self.assertIn(
            second.uuid.hex, self.listed(self.rapporteur_1, "step-checklist-responses")
        )

    def test_a_completer_who_does_not_contribute_reads_every_report(self):
        outsider = structure_factories.UserFactory()
        self.individual_step.completer = ResponsibleRoles.CALL_MANAGER
        self.individual_step.completer_proposal_role = None
        self.individual_step.save()
        self.reach_response_step()
        self.respond()
        second = self.contribute(
            self.rapporteur_2, self.individual_step, self.report_question, "No."
        )
        self.assertTrue(contributions.user_can_read(self.manager, second))
        self.assertFalse(contributions.user_can_read(self.rapporteur_1, second))
        self.assertFalse(contributions.user_can_read(outsider, second))


class ReleasedFeedbackTest(StepInputsTestMixin, test.APITestCase):
    """The reason given on completing a report released to the applicant is
    the call team's, then also the applicant team's and the step's holders'
    once released; never the other evaluators' nor the providers'."""

    REASON = "Consolidated: strong science, weak scaling plan."

    def setUp(self):
        super().setUp()
        self.reach_consolidated_step()
        instance = self.instance(self.consolidated_step)
        instance.outcome_reason = self.REASON
        instance.feedback_withheld = True
        instance.internal_notes = "Discussed at length."
        instance.save()
        self.other_reviewer = structure_factories.UserFactory()
        factories.ReviewFactory(proposal=self.proposal, reviewer=self.other_reviewer)
        self.provider = self.fixture.offering_fixture.offering_manager
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.fixture.requested_offering_accepted,
        )
        self.organiser = self.fixture.call_organizer_user

    def state(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(self.url("workflow_states"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return next(s for s in response.data if s["step"] == "consolidated-report")

    def test_who_reads_the_feedback_before_and_after_the_release(self):
        call_team = (self.manager, self.organiser, self.fixture.staff)
        holders = (self.applicant, self.team_member, self.rapporteur_1)
        others = (self.other_reviewer, self.provider)
        for user in call_team:
            self.assertEqual(self.state(user)["outcome_reason"], self.REASON)
        for user in holders[:2] + others:
            self.assertEqual(self.state(user)["outcome_reason"], "")

        self.assertEqual(self.release(self.manager).status_code, status.HTTP_200_OK)
        for user in call_team + holders:
            self.assertEqual(self.state(user)["outcome_reason"], self.REASON)
        for user in others:
            state = self.state(user)
            self.assertEqual(state["outcome_reason"], "")
            self.assertIsNone(state["rejection_reason"])

    def test_the_call_organiser_reads_the_steps_as_call_managers_do(self):
        state = self.state(self.organiser)
        self.assertEqual(state["internal_notes"], "Discussed at length.")
        self.assertIsNotNone(state["completed_by"])
        self.assertEqual(
            self.state(self.manager)["internal_notes"], "Discussed at length."
        )
        self.release(self.manager)
        self.client.force_authenticate(self.organiser)
        [report] = self.client.get(self.url("released_reports")).data
        self.assertEqual(report["released_by_name"], self.manager.full_name)
        self.assertIsNone(self.state(self.applicant)["internal_notes"])


class ReplaceWorkflowStepsTest(StepInputsTestMixin, test.APITestCase):
    def test_inputs_are_checked_when_steps_are_replaced(self):
        call = factories.CallFactory()
        with self.assertRaises(DjangoValidationError):
            utils.replace_workflow_steps(
                call,
                [
                    (
                        "first",
                        {
                            "kind": StepKinds.ASSESSMENT,
                            "name": "First",
                            "order": 0,
                            "input_steps": ["second"],
                        },
                    ),
                    (
                        "second",
                        {"kind": StepKinds.ASSESSMENT, "name": "Second", "order": 1},
                    ),
                ],
            )


class AnswersUnderLockTest(StepInputsTestMixin, test.APITestCase):
    def test_answers_are_refused_once_the_slot_was_submitted_meanwhile(self):
        self.submit()
        self.complete_ok("eligible")
        review = factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.reviewer,
            state=models.Review.States.SUBMITTED,
        )
        contributions.attach_review(review)
        self.complete_ok("reviewed")
        slot = self.instance(self.questions_step).contributions.get(
            contributor=self.rapporteur_1
        )
        real_resolve = views.ProposalViewSet._resolve_step_contribution

        def submitted_meanwhile(view, request, obj, call_step, create=True):
            # The holder's submission commits once the slot was resolved.
            resolved, error = real_resolve(view, request, obj, call_step, create)
            models.StepContribution.objects.filter(pk=slot.pk).update(
                status=StepContributionStatuses.SUBMITTED
            )
            return resolved, error

        with mock.patch.object(
            views.ProposalViewSet,
            "_resolve_step_contribution",
            autospec=True,
            side_effect=submitted_meanwhile,
        ):
            response = self.answer(
                self.rapporteur_1, self.questions_step, self.question, "Late.", slot
            )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            checklist_models.Answer.objects.filter(
                question=self.question, user=self.rapporteur_1
            ).exists()
        )
