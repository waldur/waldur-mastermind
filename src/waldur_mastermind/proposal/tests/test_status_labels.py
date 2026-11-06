"""Status labels: what each audience's dashboard calls a workflow step."""

import copy
import json
import pathlib

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_templates,
    call_transfer,
    contributions,
    models,
    status_labels,
    workflow_service,
)
from waldur_mastermind.proposal import workflow_description as wd
from waldur_mastermind.proposal.enums import (
    ContributionOutcomes,
    ProposalStates,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_change_requests import ChangeRequestBase
from waldur_mastermind.proposal.tests.test_conditional_and_parallel_steps import (
    ParallelStageBase,
)

ADMIN_LABELS = {
    "applicant": {
        "active": "Under administrative check",
        "changes_requested": "Changes requested",
        "outcomes": {
            "eligible": "Under evaluation",
            "ineligible": "Administratively rejected",
        },
    },
    "evaluators": {
        "active": "Pending administrative check",
        "changes_requested": "Sent back to the applicant",
        "completed": "Administrative check done",
    },
}


class StatusLabelConfigurationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.step = self.call.workflow_steps.get(key="administrative_check")
        self.url = factories.CallWorkflowStepFactory.get_url(self.call, self.step)
        self.client.force_authenticate(self.fixture.call_manager)

    def patch(self, labels):
        return self.client.patch(self.url, {"status_labels": labels}, format="json")

    def test_call_manager_configures_labels(self):
        labels = copy.deepcopy(ADMIN_LABELS)
        labels["applicant"]["active"] = "  Under administrative check  "
        labels["evaluators"]["outcomes"] = {"eligible": ""}
        response = self.patch(labels)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.step.refresh_from_db()
        # Stripped, and an empty text keeps the computed label.
        self.assertEqual(self.step.status_labels, ADMIN_LABELS)
        self.assertEqual(response.data["status_labels"], ADMIN_LABELS)

    def test_null_clears_the_labels(self):
        self.patch(ADMIN_LABELS)
        response = self.patch(None)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.step.refresh_from_db()
        self.assertEqual(self.step.status_labels, {})

    def test_invalid_labels_are_refused(self):
        for labels in (
            {"reviewers": {"active": "x"}},
            {"applicant": {"pending": "x"}},
            {"applicant": {"outcomes": {"approved": "x"}}},
            {"applicant": {"active": "x" * 256}},
            {"applicant": {"active": 3}},
            {"applicant": "x"},
            ["x"],
        ):
            with self.subTest(labels=labels):
                response = self.patch(labels)
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
                self.assertIn("status_labels", response.data)
        self.step.refresh_from_db()
        self.assertEqual(self.step.status_labels, {})

    def test_changing_the_outcome_set_checks_the_outcome_labels(self):
        self.patch({"applicant": {"outcomes": {"eligible": "OK"}}})
        response = self.client.patch(
            self.url, {"outcome_set": "approval"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status_labels", response.data)

    def test_applicant_cannot_configure_labels(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.patch(ADMIN_LABELS)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_labels_travel_with_an_exported_call(self):
        self.patch(ADMIN_LABELS)
        document, _ = call_transfer.export_call(self.call)
        copied, _, _ = call_transfer.import_call(
            document, self.call.manager, self.fixture.staff
        )
        self.assertEqual(
            copied.workflow_steps.get(key="administrative_check").status_labels,
            ADMIN_LABELS,
        )

    def test_import_refuses_invalid_labels(self):
        document, _ = call_transfer.export_call(self.call)
        step = next(
            s for s in document["workflow_steps"] if s["key"] == "administrative_check"
        )
        step["status_labels"] = {"applicant": {"outcomes": {"approved": "x"}}}
        with self.assertRaises(Exception):
            call_transfer.import_call(document, self.call.manager, self.fixture.staff)


class StatusLabelReadTest(ChangeRequestBase):
    """A proposal at an administrative check that labels itself."""

    def setUp(self):
        super().setUp()
        self.call_step.applicant_visible = True
        self.call_step.status_labels = ADMIN_LABELS
        self.call_step.save()

    def step_label(self, user, key="administrative_check"):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, "workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return next(s for s in response.data if s["step"] == key)["status_label"]

    def list_labels(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(factories.ProposalFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = [r for r in response.data if r["uuid"] == self.proposal.uuid.hex]
        return rows[0]["status_labels"] if rows else None

    def detail_labels(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["status_labels"]

    def test_each_audience_reads_its_own_label(self):
        self.assertEqual(self.step_label(self.applicant), "Under administrative check")
        self.assertEqual(
            self.step_label(self.call_manager), "Pending administrative check"
        )
        self.assertEqual(
            self.list_labels(self.applicant),
            [{"step": "administrative_check", "label": "Under administrative check"}],
        )
        self.assertEqual(
            self.list_labels(self.call_manager),
            [
                {
                    "step": "administrative_check",
                    "label": "Pending administrative check",
                }
            ],
        )

    def test_unrelated_user_reads_no_labels(self):
        self.assertIsNone(self.list_labels(structure_factories.UserFactory()))

    def test_labels_while_changes_are_requested(self):
        response = self.request_changes()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.step_label(self.applicant), "Changes requested")
        self.assertEqual(
            self.step_label(self.call_manager), "Sent back to the applicant"
        )
        self.assertEqual(
            self.detail_labels(self.applicant)[0]["label"], "Changes requested"
        )
        self.assertEqual(
            self.detail_labels(self.call_manager)[0]["label"],
            "Sent back to the applicant",
        )

    def test_hidden_step_keeps_the_applicant_stand_in(self):
        self.call_step.applicant_visible = False
        self.call_step.save()
        self.assertIsNone(self.step_label(self.applicant))
        self.assertEqual(self.list_labels(self.applicant), [])
        self.assertEqual(
            self.step_label(self.call_manager), "Pending administrative check"
        )

    def test_outcome_label_once_the_step_ends(self):
        self.instance.status = WorkflowStepInstanceStatuses.COMPLETED
        self.instance.outcome = WorkflowStepOutcomes.ELIGIBLE
        self.instance.save()
        self.assertEqual(self.step_label(self.applicant), "Under evaluation")
        self.assertEqual(
            self.step_label(self.call_manager), "Administrative check done"
        )
        self.assertEqual(
            self.detail_labels(self.applicant)[0]["label"], "Under evaluation"
        )

    def test_a_withheld_review_verdict_picks_no_outcome_label(self):
        review = models.CallWorkflowStep.objects.get(
            call=self.call, key="expert_review"
        )
        review.applicant_visible = True
        review.status_labels = {
            "applicant": {
                "completed": "Reviewed",
                "outcomes": {"reviewed": "Reviews in"},
            },
            "evaluators": {"outcomes": {"reviewed": "Reviews in"}},
        }
        review.save()
        self.call.reviews_visible_to_submitters = False
        self.call.save()
        self.proposal.workflow_step_instances.filter(
            step="administrative_check"
        ).update(
            status=WorkflowStepInstanceStatuses.COMPLETED,
            outcome=WorkflowStepOutcomes.ELIGIBLE,
        )
        instance = self.proposal.workflow_step_instances.get(step="expert_review")
        instance.status = WorkflowStepInstanceStatuses.COMPLETED
        instance.outcome = WorkflowStepOutcomes.REVIEWED
        instance.save()
        self.proposal.current_step = instance
        self.proposal.save()

        self.assertEqual(self.step_label(self.applicant, "expert_review"), "Reviewed")
        self.assertEqual(
            self.step_label(self.call_manager, "expert_review"), "Reviews in"
        )
        self.assertEqual(
            self.list_labels(self.applicant),
            [{"step": "expert_review", "label": "Reviewed"}],
        )
        self.assertEqual(
            self.list_labels(self.call_manager),
            [{"step": "expert_review", "label": "Reviews in"}],
        )

    def test_a_held_decision_reads_as_still_being_made(self):
        decision = models.CallWorkflowStep.objects.get(
            call=self.call, key="allocation_decision"
        )
        # Hidden from the applicant, whose outcome is theirs all the same.
        self.assertFalse(decision.applicant_visible)
        decision.status_labels = {
            "applicant": {
                "active": "Awaiting the decision",
                "outcomes": {"approved": "Awarded (tentative)"},
            },
            "evaluators": {"outcomes": {"approved": "Awarded (tentative)"}},
        }
        decision.save()
        self.proposal.workflow_step_instances.exclude(
            step="allocation_decision"
        ).update(status=WorkflowStepInstanceStatuses.COMPLETED)
        instance = self.proposal.workflow_step_instances.get(step="allocation_decision")
        instance.status = WorkflowStepInstanceStatuses.COMPLETED
        instance.outcome = WorkflowStepOutcomes.APPROVED
        instance.save()
        self.proposal.current_step = instance
        self.proposal.decision_held = True
        self.proposal.save()

        self.assertEqual(
            self.step_label(self.applicant, "allocation_decision"),
            "Awaiting the decision",
        )
        self.assertEqual(
            self.list_labels(self.applicant),
            [{"step": "allocation_decision", "label": "Awaiting the decision"}],
        )
        self.assertEqual(
            self.list_labels(self.call_manager),
            [{"step": "allocation_decision", "label": "Awarded (tentative)"}],
        )

        self.proposal.decision_held = False
        self.proposal.save()
        self.assertEqual(
            self.step_label(self.applicant, "allocation_decision"),
            "Awarded (tentative)",
        )

    def test_no_labels_without_configuration(self):
        self.call_step.status_labels = {}
        self.call_step.save()
        self.assertIsNone(self.step_label(self.applicant))
        self.assertEqual(self.list_labels(self.call_manager), [])

    def test_the_list_pays_no_query_per_proposal(self):
        def count():
            self.client.force_authenticate(self.call_manager)
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(factories.ProposalFactory.get_list_url())
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            return len(queries)

        def with_and_without_labels():
            labelled = count()
            self.call_step.status_labels = {}
            self.call_step.save()
            plain = count()
            self.call_step.status_labels = ADMIN_LABELS
            self.call_step.save()
            return labelled, plain

        one_labelled, one_plain = with_and_without_labels()
        other = factories.ProposalFactory(
            round=self.fixture.round, state=ProposalStates.SUBMITTED
        )
        fixtures.author_as_manager(other)
        workflow_service.create_step_instances(other)
        workflow_service.activate_first_step(other)
        two_labelled, two_plain = with_and_without_labels()
        # A second proposal costs no more for its labels.
        self.assertLessEqual(two_labelled - one_labelled, two_plain - one_plain)


class ParallelStageLabelTest(ParallelStageBase):
    def setUp(self):
        super().setUp()
        self.technical.status_labels = {
            "evaluators": {
                "active": "Under Technical Assessment",
                "completed": "Technical Assessment Submitted",
            }
        }
        self.technical.save()
        self.panel.status_labels = {"evaluators": {"active": "Pending panel review"}}
        self.panel.save()

    def labels(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [entry["label"] for entry in response.data["status_labels"]]

    def test_every_step_of_the_stage_shows_its_label(self):
        self.assertEqual(
            self.labels(), ["Under Technical Assessment", "Pending panel review"]
        )
        self._assess_all(ContributionOutcomes.ACCEPTED)
        self.assertEqual(
            self.labels(), ["Technical Assessment Submitted", "Pending panel review"]
        )

    def test_an_offering_manager_reads_only_their_own_steps_label(self):
        self.client.force_authenticate(self.manager_a)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, "workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        labels = {s["step"]: s["status_label"] for s in response.data}
        self.assertEqual(labels["technical_assessment"], "Under Technical Assessment")
        self.assertIsNone(labels["panel_review"])


class AfterTheAwardLabelTest(SimpleTestCase):
    def test_the_decision_on_an_extension_or_final_report_reaches_the_applicant(self):
        for phase in ("extension", "closing"):
            decision = {"kind": "decision", "phase": phase}
            self.assertTrue(status_labels.applies_to_applicant(decision))
            self.assertFalse(
                status_labels.applies_to_applicant(
                    {"kind": "assessment", "phase": phase}
                )
            )
        self.assertFalse(
            status_labels.applies_to_applicant(
                {"kind": "decision", "phase": "evaluation"}
            )
        )

    def test_every_template_labels_its_steps_after_the_award(self):
        """The tender's rows for extension requests and the final report."""
        for name in call_templates.names():
            with self.subTest(template=name):
                description = wd.describe(call_templates.load(name))
                after = [s for s in description["steps"] if s["phase"] != "evaluation"]
                self.assertTrue(after)
                for step in after:
                    self.assertTrue(
                        step["status_labels"].get("evaluators"), step["key"]
                    )
                labels = {s["key"]: s["applicant"]["labels"] for s in after}
                # A preset may name its report for what it reports on, such
                # as an experiment report.
                (report,) = [s["name"] for s in after if s["key"] == "final_report"]
                self.assertEqual(
                    labels["final_report"],
                    {
                        "active": f"Pending {report.lower()}",
                        "completed": f"{report} submitted",
                    },
                )
                self.assertEqual(
                    labels["final_report_approval"]["outcomes"],
                    {"approved": "Project completed"},
                )
                if "extension_decision" in labels:
                    # A refused extension leaves the project as awarded.
                    self.assertEqual(
                        labels["extension_decision"]["outcomes"],
                        {"approved": "Project extended", "declined": "Awarded"},
                    )
                self.assertNotIn(
                    "applicant_labels_on_hidden_step",
                    [f["code"] for f in description["notifications"]["flags"]],
                )


class AudienceAndCountConfigurationTest(StatusLabelConfigurationTest):
    def test_call_team_and_offering_team_labels_are_kept(self):
        labels = {
            "evaluators": {"active": "Pending administrative check"},
            "call_team": {"active": "{submitted} of {expected} checks in"},
            "offering_team": {"completed": "Checked"},
        }
        response = self.patch(labels)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.step.refresh_from_db()
        self.assertEqual(self.step.status_labels, labels)

    def test_placeholders_are_checked(self):
        for labels in (
            {"call_team": {"active": "{reviewers} invited"}},
            {"evaluators": {"outcomes": {"eligible": "{0} done"}}},
            # The applicant is not told how their evaluation is staffed.
            {"applicant": {"active": "{submitted} reviews in"}},
        ):
            with self.subTest(labels=labels):
                response = self.patch(labels)
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
                self.assertIn("status_labels", response.data)
        self.step.refresh_from_db()
        self.assertEqual(self.step.status_labels, {})


class AudienceFallbackTest(SimpleTestCase):
    def test_call_team_and_offering_team_fall_back_to_the_evaluators(self):
        step = {
            "status_labels": {
                "evaluators": {
                    "active": "Under Assessment",
                    "completed": "Assessed",
                    "outcomes": {"accepted": "Accepted", "rejected": "Rejected"},
                },
                "call_team": {"active": "Under Assessment ({submitted})"},
                "offering_team": {"outcomes": {"accepted": "Feasible"}},
            }
        }
        self.assertEqual(
            status_labels.configured(step, status_labels.CALL_TEAM),
            {
                "active": "Under Assessment ({submitted})",
                "completed": "Assessed",
                "outcomes": {"accepted": "Accepted", "rejected": "Rejected"},
            },
        )
        self.assertEqual(
            status_labels.configured(step, status_labels.OFFERING_TEAM)["outcomes"],
            {"accepted": "Feasible", "rejected": "Rejected"},
        )
        self.assertEqual(
            status_labels.configured(
                {"status_labels": {"call_team": {"active": "Mine"}}},
                status_labels.EVALUATORS,
            ),
            {},
        )

    def test_render_fills_only_known_counts(self):
        self.assertEqual(
            status_labels.render(
                "{submitted} of {expected} in", {"submitted": 1, "expected": 3}
            ),
            "1 of 3 in",
        )
        calls = []
        self.assertEqual(
            status_labels.render("No counts", lambda: calls.append(1)), "No counts"
        )
        # Counts are worked out only for a label that shows them.
        self.assertEqual(calls, [])


class CallTeamLabelReadTest(ChangeRequestBase):
    step_label = StatusLabelReadTest.step_label
    list_labels = StatusLabelReadTest.list_labels

    def setUp(self):
        super().setUp()
        self.call_step.applicant_visible = True
        labels = copy.deepcopy(ADMIN_LABELS)
        labels["call_team"] = {"active": "Admin check pending"}
        self.call_step.status_labels = labels
        self.call_step.save()

    def test_the_call_team_reads_its_own_labels(self):
        self.assertEqual(self.step_label(self.call_manager), "Admin check pending")
        self.assertEqual(self.step_label(self.fixture.staff), "Admin check pending")
        self.assertEqual(
            self.list_labels(self.call_manager),
            [{"step": "administrative_check", "label": "Admin check pending"}],
        )
        self.assertEqual(self.step_label(self.applicant), "Under administrative check")

    def test_the_call_team_falls_back_to_the_evaluators_label(self):
        response = self.request_changes()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.step_label(self.call_manager), "Sent back to the applicant"
        )


class OfferingTeamAndCountLabelTest(ParallelStageBase):
    def setUp(self):
        super().setUp()
        self.technical.status_labels = {
            "evaluators": {
                "active": "Under Assessment",
                "completed": "Technical Assessment Submitted",
            },
            "call_team": {
                "active": "Under Assessment: {submitted} of {expected} partitions "
                "assessed"
            },
            "offering_team": {"active": "Under Technical Assessment"},
        }
        self.technical.save()

    def states(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, "workflow_states")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {s["step"]: s["status_label"] for s in response.data}

    def list_labels(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(factories.ProposalFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = [r for r in response.data if r["uuid"] == self.proposal.uuid.hex]
        return rows[0]["status_labels"] if rows else None

    def test_each_side_reads_its_own_label(self):
        self.assertEqual(
            self.states(self.manager_a)["technical_assessment"],
            "Under Technical Assessment",
        )
        self.assertEqual(
            self.states(self.call_manager)["technical_assessment"],
            "Under Assessment: 0 of 2 partitions assessed",
        )
        self.assertEqual(
            self.states(self.panel_member)["technical_assessment"], "Under Assessment"
        )

    def test_the_counts_follow_the_work(self):
        self._assess(
            "technical_assessment",
            self.requested_a,
            self.manager_a,
            ContributionOutcomes.ACCEPTED,
        )
        self.assertEqual(
            self.states(self.call_manager)["technical_assessment"],
            "Under Assessment: 1 of 2 partitions assessed",
        )

    def test_the_lists_show_the_readers_label(self):
        self.assertEqual(
            self.list_labels(self.manager_a),
            [{"step": "technical_assessment", "label": "Under Technical Assessment"}],
        )
        self.assertIn(
            {
                "step": "technical_assessment",
                "label": "Under Assessment: 0 of 2 partitions assessed",
            },
            self.list_labels(self.call_manager),
        )

    def test_a_provider_reads_no_label_of_a_step_it_does_not_work_on(self):
        self.panel.status_labels = {"evaluators": {"active": "Pending panel review"}}
        self.panel.save()
        self.assertIsNone(self.states(self.manager_a)["panel_review"])
        self.assertNotIn(
            "panel_review",
            [entry["step"] for entry in self.list_labels(self.manager_a)],
        )


class StepCountsTest(ChangeRequestBase):
    def test_reviews_and_invitations_are_counted(self):
        review_step = models.CallWorkflowStep.objects.get(
            call=self.call, key="expert_review"
        )
        review_step.min_contributions = 3
        review_step.save()
        instance = self.proposal.workflow_step_instances.get(step="expert_review")
        instance.status = WorkflowStepInstanceStatuses.ACTIVE
        instance.save()
        submitted = factories.ReviewFactory(proposal=self.proposal)
        submitted.state = models.Review.States.SUBMITTED
        submitted.save()
        factories.ReviewFactory(proposal=self.proposal)
        declined = factories.ReviewFactory(proposal=self.proposal)
        declined.state = models.Review.States.REJECTED
        declined.save()
        for item_status in ("pending", "declined", "expired", "coi_blocked"):
            batch = factories.AssignmentBatchFactory(
                call=self.call, call_step=review_step, status="sent"
            )
            factories.AssignmentItemFactory(
                batch=batch, proposal=self.proposal, status=item_status
            )
        draft = factories.AssignmentBatchFactory(
            call=self.call, call_step=review_step, status="draft"
        )
        factories.AssignmentItemFactory(batch=draft, proposal=self.proposal)

        self.assertEqual(
            contributions.step_counts(instance),
            {
                # Three reviews, a pending and an expired invitation, and
                # one declined; neither a blocked one nor a draft's.
                "invited": 6,
                "accepted": 2,
                "declined": 2,
                "submitted": 1,
                "expected": 3,
                "reports_submitted": 1,
                "reports_expected": 3,
            },
        )


class SharedPlaceholderFixtureTest(SimpleTestCase):
    """The cases homeport pins for the step dialog's own check."""

    def test_the_shared_cases(self):
        fixture = json.loads(
            (
                pathlib.Path(__file__).parent / "status_label_placeholders.json"
            ).read_text()
        )
        self.assertEqual(fixture["audiences"], list(status_labels.AUDIENCES))
        self.assertEqual(fixture["placeholders"], list(status_labels.PLACEHOLDERS))
        for case in fixture["cases"]:
            with self.subTest(case=case):
                labels = {case["audience"]: {"active": case["text"]}}
                if case["valid"]:
                    self.assertEqual(status_labels.clean(labels, "eligibility"), labels)
                else:
                    with self.assertRaises(DjangoValidationError):
                        status_labels.clean(labels, "eligibility")
