"""The workflow checker: rules over the workflow description, the activation
gate, and who reads the findings."""

import copy
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import (
    call_templates,
    call_transfer,
    models,
)
from waldur_mastermind.proposal import workflow_checks as wc
from waldur_mastermind.proposal.enums import (
    CallStates,
    RequestedOfferingStates,
)
from waldur_mastermind.proposal.tests import factories, fixtures


def step(key, kind="decision", outcome_set="eligibility", **values):
    return {
        "key": key,
        "name": key.replace("_", " ").capitalize(),
        "kind": kind,
        "outcome_set": outcome_set,
        "completer": "call_manager",
        "duration_in_days": 7,
        **values,
    }


def decision(**values):
    return step(
        "allocation_decision",
        outcome_set="approval",
        is_allocation_decision=True,
        **values,
    )


def document(steps, roles=(), rounds=None, **call):
    data = {
        "schema_version": 3,
        "call": {"name": "Call", **call},
        "proposal_roles": list(roles),
        "workflow_steps": [{"order": index, **s} for index, s in enumerate(steps)],
    }
    if rounds is not None:
        data["rounds"] = rounds
    return data


def base():
    return document([step("administrative_check"), decision()])


def role(key, **values):
    return {
        "key": key,
        "name": key.capitalize(),
        "eligible_from": "call_role:panel_member",
        "min_holders": 1,
        "max_holders": 2,
        "allows_lead": False,
        **values,
    }


def found(data, code, step_key=None, call=None):
    return [
        f
        for f in wc.check(data, call)
        if f["code"] == code and (step_key is None or f["step"] == step_key)
    ]


class BaseWorkflowTest(SimpleTestCase):
    def test_a_plain_workflow_has_no_errors(self):
        self.assertEqual(wc.errors(wc.check(base())), [])

    def test_findings_have_their_shape_and_errors_come_first(self):
        data = base()
        data["workflow_steps"][0]["duration_in_days"] = None
        data["workflow_steps"][1]["is_enabled"] = False
        findings = wc.check(data)
        self.assertEqual(findings[0]["severity"], wc.ERROR)
        for finding in findings:
            self.assertEqual(
                set(finding),
                {
                    "code",
                    "title",
                    "severity",
                    "step",
                    "step_name",
                    "field",
                    "message",
                    "because",
                },
            )

    def test_every_rule_and_flag_code_has_its_own_title(self):
        codes = [r.code for r in wc.RULES] + list(wc.FLAG_CODES.values())
        titles = {code: wc.title(code) for code in codes}
        self.assertTrue(all(titles.values()))
        # One name per code; an unreachable flag reports under its rule's
        # own code, which is why the codes are counted as a set.
        self.assertEqual(len(set(titles.values())), len(set(codes)))

    def test_a_finding_carries_its_rule_title(self):
        data = base()
        data["workflow_steps"][0]["duration_in_days"] = None
        [finding] = found(data, "step_without_duration")
        self.assertEqual(finding["title"], "No duration")


class ActivationRulesTest(SimpleTestCase):
    def test_no_enabled_step(self):
        data = base()
        for s in data["workflow_steps"]:
            s["is_enabled"] = False
        self.assertTrue(found(data, "no_enabled_step"))
        self.assertFalse(found(base(), "no_enabled_step"))

    def test_decision_missing(self):
        data = document([step("administrative_check")])
        [finding] = found(data, "decision_missing")
        self.assertEqual(
            finding["message"],
            "The call has no allocation decision step. Every call needs one: add it.",
        )
        self.assertIsNone(finding["step"])
        self.assertFalse(found(base(), "decision_missing"))

    def test_a_disabled_decision_is_told_apart_from_a_missing_one(self):
        data = base()
        decision_step = next(
            s for s in data["workflow_steps"] if s.get("is_allocation_decision")
        )
        decision_step["is_enabled"] = False
        [finding] = found(data, "decision_missing")
        self.assertEqual(
            finding["message"],
            "The allocation decision step is disabled. Every call needs one: "
            "enable it.",
        )
        self.assertEqual(finding["step"], decision_step["key"])

    def test_decision_multiple(self):
        data = base()
        data["workflow_steps"].append(
            {**decision(), "key": "board_decision", "order": 5}
        )
        findings = found(data, "decision_multiple")
        self.assertEqual(len(findings), 2)
        self.assertIn("it has 2", findings[0]["message"])
        self.assertFalse(found(base(), "decision_multiple"))

    def test_award_before_decision(self):
        data = document(
            [
                step("administrative_check"),
                step(
                    "award_response", kind="applicant_action", outcome_set="acceptance"
                ),
                decision(),
            ]
        )
        finding = found(data, "award_before_decision")[0]
        self.assertEqual(
            (finding["step"], finding["field"]), ("award_response", "order")
        )
        self.assertIn("after the allocation decision", finding["message"])
        data["workflow_steps"][1]["order"] = 9
        self.assertFalse(found(data, "award_before_decision"))

    def test_automatic_decision_unguarded(self):
        data = document([decision(decides_automatically=True)])
        self.assertTrue(found(data, "automatic_decision_unguarded"))
        data = document(
            [step("administrative_check"), decision(decides_automatically=True)]
        )
        self.assertFalse(found(data, "automatic_decision_unguarded"))

    def test_automatic_decision_relying_on_the_provider_needs_no_earlier_step(self):
        data = document(
            [
                decision(
                    decides_automatically=True,
                    automatic_decision_guard="provider_fulfilment",
                )
            ]
        )
        self.assertFalse(found(data, "automatic_decision_unguarded"))

    def test_applicant_step_without_form(self):
        data = base()
        data["workflow_steps"].insert(
            1,
            step(
                "response",
                kind="applicant_action",
                outcome_set="submitted",
                completer="applicant",
                order=1,
            ),
        )
        finding = found(data, "applicant_step_without_form")[0]
        self.assertEqual(finding["field"], "checklist")
        self.assertIn("have no form", finding["message"])
        self.assertIn("Response", finding["message"])
        data["workflow_steps"][1]["checklist"] = {"name": "Form"}
        self.assertFalse(found(data, "applicant_step_without_form"))


class WhoDoesTheWorkTest(SimpleTestCase):
    def test_completer_role_missing(self):
        data = document(
            [
                step(
                    "panel_report",
                    kind="assessment",
                    outcome_set="reviewed",
                    completer="proposal_role",
                    completer_proposal_role="rapporteur",
                ),
                decision(),
            ]
        )
        finding = found(data, "completer_unholdable", "panel_report")[0]
        self.assertEqual(finding["field"], "completer_proposal_role")
        data["proposal_roles"] = [role("rapporteur")]
        self.assertFalse(found(data, "completer_unholdable"))

    def test_lead_completion_on_a_role_without_lead(self):
        roles = [role("rapporteur")]
        data = document(
            [
                step(
                    "report",
                    kind="assessment",
                    outcome_set="reviewed",
                    completer="proposal_role",
                    completer_proposal_role="rapporteur",
                    contributor_source="proposal_role:rapporteur",
                    contributors_lead_only=True,
                ),
                decision(),
            ],
            roles,
        )
        fields = {f["field"] for f in found(data, "completer_unholdable")}
        self.assertIn("contributors_lead_only", fields)
        data["proposal_roles"][0]["allows_lead"] = True
        self.assertFalse(found(data, "completer_unholdable"))

    def test_role_of_reviewers_without_a_review_before(self):
        roles = [role("consensus", eligible_from="proposal_reviewers")]
        consensus = step(
            "consensus",
            kind="assessment",
            outcome_set="reviewed",
            completer="proposal_role",
            completer_proposal_role="consensus",
        )
        data = document([consensus, decision()], roles)
        self.assertTrue(found(data, "completer_unholdable", "consensus"))
        review = step(
            "review",
            kind="assessment",
            outcome_set="reviewed",
            contributor_source="assigned_reviewers",
        )
        data = document([review, consensus, decision()], roles)
        self.assertFalse(found(data, "completer_unholdable"))

    def test_role_assigned_after_the_step(self):
        roles = [role("rapporteur")]
        report = step(
            "report",
            kind="assessment",
            outcome_set="reviewed",
            completer="proposal_role",
            completer_proposal_role="rapporteur",
        )
        assignment = step(
            "assign",
            kind="assignment",
            outcome_set="done",
            assigns_roles=["rapporteur"],
        )
        data = document([report, assignment, decision()], roles)
        self.assertIn(
            "only after", found(data, "completer_unholdable", "report")[0]["message"]
        )
        data = document([assignment, report, decision()], roles)
        self.assertFalse(found(data, "completer_unholdable"))
        self.assertFalse(found(data, "role_assigned_by_hand"))

    def test_role_nobody_assigns_is_assigned_by_hand(self):
        report = step(
            "report",
            kind="assessment",
            outcome_set="reviewed",
            completer="proposal_role",
            completer_proposal_role="rapporteur",
        )
        data = document([report, decision()], [role("rapporteur")])
        finding = found(data, "role_assigned_by_hand")[0]
        self.assertEqual(finding["severity"], wc.INFO)

    def test_more_contributions_than_holders(self):
        roles = [role("expert", max_holders=2)]
        data = document(
            [
                step(
                    "review",
                    kind="assessment",
                    outcome_set="reviewed",
                    contributor_source="proposal_role:expert",
                    min_contributions=3,
                ),
                decision(),
            ],
            roles,
        )
        finding = found(data, "contributors_unholdable", "review")[0]
        self.assertEqual(finding["field"], "min_contributions")
        data["workflow_steps"][0]["min_contributions"] = 2
        self.assertFalse(found(data, "contributors_unholdable"))

    def test_lead_only_contributors_without_lead(self):
        data = document(
            [
                step(
                    "review",
                    kind="assessment",
                    outcome_set="reviewed",
                    contributor_source="proposal_role:expert",
                    contributors_lead_only=True,
                ),
                decision(),
            ],
            [role("expert")],
        )
        self.assertTrue(found(data, "contributors_unholdable", "review"))

    def test_delegation_to_offering_team_on_a_per_person_step(self):
        data = document(
            [
                step(
                    "review",
                    kind="assessment",
                    outcome_set="reviewed",
                    contributor_source="proposal_role:expert",
                    delegable_to="offering_team",
                ),
                decision(),
            ],
            [role("expert"), role("deputy")],
        )
        self.assertTrue(found(data, "delegation_unholdable", "review"))
        data["workflow_steps"][0]["delegable_to"] = "proposal_role:deputy"
        self.assertFalse(found(data, "delegation_unholdable"))
        data["workflow_steps"][0]["delegable_to"] = "proposal_role:missing"
        self.assertTrue(found(data, "delegation_unholdable", "review"))


def reviews(key="scientific_review", **values):
    return step(
        key,
        kind="assessment",
        outcome_set="reviewed",
        contributor_source="assigned_reviewers",
        **values,
    )


class UnmeetableMinimumsTest(SimpleTestCase):
    def test_applicant_completing_a_call_team_step(self):
        data = document([step("eligibility_check", completer="applicant"), decision()])
        finding = found(data, "applicant_completes_evaluation", "eligibility_check")[0]
        self.assertEqual(finding["severity"], wc.ERROR)
        self.assertEqual(finding["field"], "completer")
        data["workflow_steps"][0].update(
            kind="applicant_action", outcome_set="submitted"
        )
        self.assertFalse(found(data, "applicant_completes_evaluation"))

    def test_minimum_of_reviews_with_no_review_step_before(self):
        data = document([step("eligibility_check", min_contributions=1), decision()])
        finding = found(data, "minimum_without_reviews", "eligibility_check")[0]
        self.assertEqual(finding["severity"], wc.WARNING)
        self.assertIn("by hand", finding["message"])
        data["workflow_steps"].insert(0, reviews(min_contributions=2))
        self.assertFalse(found(data, "minimum_without_reviews"))

    def test_minimum_of_reviews_above_what_review_steps_ask(self):
        data = document([reviews(min_contributions=2), decision(min_contributions=3)])
        finding = found(data, "minimum_without_reviews", "allocation_decision")[0]
        self.assertIn("ask for 2", finding["message"])
        data["workflow_steps"][1]["min_contributions"] = 2
        self.assertFalse(found(data, "minimum_without_reviews"))

    def test_review_step_after_the_minimum_does_not_count(self):
        data = document([decision(min_contributions=1), reviews(min_contributions=2)])
        self.assertTrue(found(data, "minimum_without_reviews", "allocation_decision"))

    def test_per_offering_minimum(self):
        data = document(
            [
                step(
                    "technical_assessment",
                    kind="assessment",
                    outcome_set="feasibility",
                    contributor_source="requested_offering_team",
                    per_requested_offering=True,
                    min_contributions=2,
                ),
                decision(),
            ]
        )
        finding = found(data, "per_offering_minimum", "technical_assessment")[0]
        self.assertEqual(finding["severity"], wc.WARNING)
        data["workflow_steps"][0]["min_contributions"] = 1
        self.assertFalse(found(data, "per_offering_minimum"))

    def test_score_gate_with_nothing_scoring(self):
        gate = step(
            "quality_threshold",
            min_score_threshold="3.00",
            input_steps=["scientific_review"],
        )
        data = document([reviews(), gate, decision()])
        self.assertFalse(found(data, "score_gate_without_scores"))
        data["workflow_steps"][0]["is_enabled"] = False
        finding = found(data, "score_gate_without_scores", "quality_threshold")[0]
        self.assertEqual(finding["severity"], wc.ERROR)


class OrderTest(SimpleTestCase):
    def conditional(self, condition, **values):
        return document(
            [
                step(
                    "technical", kind="assessment", outcome_set="feasibility", **values
                ),
                step(
                    "emulation",
                    kind="assessment",
                    outcome_set="done",
                    run_condition=condition,
                ),
                decision(),
            ]
        )

    def test_condition_bad_reference(self):
        for condition in (
            {"step": "missing", "outcomes": ["feasible"]},
            {"step": "technical", "outcomes": ["nonsense"]},
        ):
            with self.subTest(condition=condition):
                data = self.conditional(condition)
                self.assertTrue(found(data, "condition_bad_reference", "emulation"))
        data = document(
            [
                step(
                    "first",
                    kind="assessment",
                    outcome_set="done",
                    run_condition={"step": "second", "outcomes": ["done"]},
                ),
                step("second", kind="assessment", outcome_set="done"),
                decision(),
            ]
        )
        self.assertTrue(found(data, "condition_bad_reference", "first"))
        self.assertFalse(
            found(
                self.conditional({"step": "technical", "outcomes": ["feasible"]}),
                "condition_bad_reference",
            )
        )

    def test_condition_unreachable(self):
        negative = self.conditional({"step": "technical", "outcomes": ["infeasible"]})
        self.assertTrue(found(negative, "condition_unreachable", "emulation"))
        going_on = self.conditional(
            {"step": "technical", "outcomes": ["infeasible"]}, on_negative="continue"
        )
        self.assertFalse(found(going_on, "condition_unreachable"))
        disabled = self.conditional(
            {"step": "technical", "outcomes": ["feasible"]}, is_enabled=False
        )
        self.assertTrue(found(disabled, "condition_unreachable", "emulation"))

    def test_input_not_earlier(self):
        data = document(
            [
                step("technical", kind="assessment", outcome_set="feasibility"),
                step(
                    "scientific",
                    kind="assessment",
                    outcome_set="reviewed",
                    parallel_with_previous=True,
                    input_steps=["technical"],
                ),
                decision(),
            ]
        )
        self.assertIn(
            "alongside",
            found(data, "input_not_earlier", "scientific")[0]["message"],
        )
        data["workflow_steps"][1]["parallel_with_previous"] = False
        self.assertFalse(found(data, "input_not_earlier"))

    def test_stage_forbidden_member(self):
        data = document(
            [
                step("technical", kind="assessment", outcome_set="feasibility"),
                decision(parallel_with_previous=True),
            ]
        )
        self.assertTrue(found(data, "stage_forbidden_member", "allocation_decision"))
        self.assertFalse(found(base(), "stage_forbidden_member"))


class NotificationRulesTest(SimpleTestCase):
    def test_a_rule_to_the_lead_of_a_role_without_one_reaches_nobody(self):
        data = document(
            [
                step(
                    "report",
                    kind="assessment",
                    outcome_set="reviewed",
                    notification_rules=[
                        {
                            "trigger": "step_started",
                            "recipient": "proposal_role_lead",
                            "proposal_role": "expert",
                        }
                    ],
                ),
                decision(),
            ],
            [role("expert")],
        )
        findings = found(data, "notification_resolves_to_nobody", "report")
        self.assertTrue(any(f["severity"] == wc.ERROR for f in findings))
        data["proposal_roles"][0]["allows_lead"] = True
        self.assertFalse(
            [
                f
                for f in found(data, "notification_resolves_to_nobody")
                if f["severity"] == wc.ERROR
            ]
        )

    def test_two_rules_raising_one_flag_name_their_events(self):
        data = document(
            [
                step(
                    "report",
                    kind="assessment",
                    outcome_set="reviewed",
                    notification_rules=[
                        {
                            "trigger": trigger,
                            "recipient": "proposal_role_lead",
                            "proposal_role": "expert",
                        }
                        for trigger in ("step_started", "step_expired")
                    ],
                ),
                decision(),
            ],
            [role("expert")],
        )
        findings = found(data, "notification_resolves_to_nobody", "report")
        events = {
            ref["detail"]
            for f in findings
            for ref in f["because"]
            if ref["source"] == "rule" and ref["field"] == "trigger"
        }
        self.assertEqual(events, {"step_started", "step_expired"})

    def test_description_flags_come_through(self):
        data = document(
            [
                step(
                    "response",
                    kind="applicant_action",
                    outcome_set="submitted",
                    completer="applicant",
                    checklist={"name": "Form"},
                ),
                decision(),
            ]
        )
        finding = found(data, "notification_missing", "response")[0]
        self.assertEqual(finding["field"], "notification_rules")


class ScheduleTest(SimpleTestCase):
    def test_durations_exceed_the_target(self):
        data = base()
        data["call"]["target_decision_days"] = 10
        finding = found(data, "durations_exceed_decision_date")[0]
        self.assertIn("14 days", finding["message"])
        self.assertIn("Administrative check (7)", finding["message"])
        data["call"]["target_decision_days"] = 30
        self.assertFalse(found(data, "durations_exceed_decision_date"))

    def test_rounds_use_their_allocation_date(self):
        data = base()
        data["rounds"] = [
            {
                "start_time": "2026-01-01T00:00:00+00:00",
                "cutoff_time": "2026-02-01T00:00:00+00:00",
                "allocation_date": "2026-02-10T00:00:00+00:00",
            }
        ]
        self.assertIn(
            "round 1", found(data, "durations_exceed_decision_date")[0]["message"]
        )

    def test_step_without_duration(self):
        data = base()
        data["workflow_steps"][0]["duration_in_days"] = None
        self.assertTrue(found(data, "step_without_duration", "administrative_check"))
        self.assertFalse(found(base(), "step_without_duration"))

    def test_open_round_without_target(self):
        data = base()
        data["rounds"] = [{"start_time": "2026-01-01T00:00:00+00:00"}]
        self.assertTrue(found(data, "open_round_without_target"))
        data["call"]["target_decision_days"] = 30
        self.assertFalse(found(data, "open_round_without_target"))


class OutcomesTest(SimpleTestCase):
    def test_automatic_decision_whose_gates_are_all_conditional(self):
        data = document(
            [
                step("technical", kind="assessment", outcome_set="feasibility"),
                step(
                    "emulation",
                    kind="assessment",
                    outcome_set="feasibility",
                    run_condition={"step": "technical", "outcomes": ["feasible"]},
                ),
                decision(decides_automatically=True),
            ]
        )
        data["workflow_steps"][0]["on_negative"] = "continue"
        self.assertTrue(found(data, "automatic_decision_without_inputs"))
        data["workflow_steps"][0]["on_negative"] = "reject"
        self.assertFalse(found(data, "automatic_decision_without_inputs"))

    def test_negative_continues_on_gate(self):
        data = document(
            [
                step(
                    "technical",
                    kind="assessment",
                    outcome_set="feasibility",
                    on_negative="continue",
                ),
                decision(),
            ]
        )
        self.assertTrue(found(data, "negative_continues_on_gate", "technical"))
        data["workflow_steps"][1]["input_steps"] = ["technical"]
        self.assertFalse(found(data, "negative_continues_on_gate"))
        data["workflow_steps"][0]["min_score_threshold"] = "3.0"
        self.assertTrue(found(data, "negative_continues_on_gate", "technical"))

    def test_blind_review_identity_exposed(self):
        data = document(
            [
                step(
                    "review",
                    kind="assessment",
                    outcome_set="reviewed",
                    blind_review=True,
                    release_to_applicant=True,
                ),
                decision(),
            ],
            reviewer_identity_visible_to_submitters=True,
        )
        self.assertTrue(found(data, "blind_review_identity_exposed", "review"))
        data["call"]["reviewer_identity_visible_to_submitters"] = False
        self.assertFalse(found(data, "blind_review_identity_exposed"))


class RoundScopedStepsTest(SimpleTestCase):
    """Steps run once per round register their rules as soon as documents
    carry the field."""

    def test_per_round_on_open_round(self):
        data = document(
            [
                step("administrative_check"),
                step("panel_meeting", round_scoped=True),
                decision(),
            ],
            rounds=[{"start_time": "2026-01-01T00:00:00+00:00"}],
        )
        self.assertTrue(found(data, "per_round_on_open_round", "panel_meeting"))
        data["rounds"][0]["cutoff_time"] = "2026-02-01T00:00:00+00:00"
        self.assertFalse(found(data, "per_round_on_open_round"))

    def test_per_round_after_per_proposal(self):
        data = document(
            [
                step("administrative_check"),
                step("panel_meeting", round_scoped=True),
                decision(),
            ]
        )
        self.assertTrue(found(data, "per_round_after_per_proposal", "panel_meeting"))
        self.assertFalse(found(base(), "per_round_after_per_proposal"))


class RoundStepRulesTest(SimpleTestCase):
    def meeting(self, **values):
        return step(
            "panel_meeting",
            kind="assessment",
            outcome_set="done",
            round_scoped=True,
            **{"contributor_source": "call_role:panel_member", **values},
        )

    def test_round_step_needs_evaluation_at_the_cutoff(self):
        data = document([step("administrative_check"), self.meeting(), decision()])
        self.assertTrue(found(data, "round_step_not_at_cutoff", "panel_meeting"))
        data["call"]["evaluation_start"] = "at_cutoff"
        self.assertFalse(found(data, "round_step_not_at_cutoff"))

    def test_round_step_forbidden_settings(self):
        data = document(
            [
                step("administrative_check"),
                self.meeting(min_score_threshold="3.0", allows_change_requests=True),
                decision(),
            ]
        )
        fields = {f["field"] for f in found(data, "round_step_forbidden_setting")}
        self.assertEqual(fields, {"min_score_threshold", "allows_change_requests"})
        data["workflow_steps"][1].pop("min_score_threshold")
        data["workflow_steps"][1].pop("allows_change_requests")
        self.assertFalse(found(data, "round_step_forbidden_setting"))

    def test_round_assessment_without_contributors(self):
        data = document(
            [
                step("administrative_check"),
                self.meeting(contributor_source="none"),
                decision(),
            ]
        )
        self.assertTrue(found(data, "round_assessment_without_contributors"))
        data = document([step("administrative_check"), self.meeting(), decision()])
        self.assertFalse(found(data, "round_assessment_without_contributors"))

    def test_manual_technical_assessment_without_a_round_advance(self):
        technical = step(
            "technical",
            kind="assessment",
            outcome_set="feasibility",
            transition_mode="manual",
            round_scoped=False,
        )
        data = document([technical, decision()])
        self.assertTrue(found(data, "round_advance_missing", "technical"))
        data = document([technical, self.meeting(), decision()])
        self.assertFalse(found(data, "round_advance_missing"))

    def test_round_step_kind_source_and_phase(self):
        assignment = step(
            "panel_meeting",
            kind="assignment",
            outcome_set="done",
            round_scoped=True,
        )
        offering_meeting = self.meeting(contributor_source="requested_offering_team")
        offering_meeting["key"] = "offering_meeting"
        data = document(
            [step("administrative_check"), assignment, offering_meeting, decision()]
        )
        fields = {
            (f["step"], f["field"]) for f in found(data, "round_step_forbidden_setting")
        }
        self.assertIn(("panel_meeting", "round_scoped"), fields)
        self.assertIn(("offering_meeting", "contributor_source"), fields)
        after_award = document(
            [step("administrative_check"), decision(), self.meeting(phase="closing")]
        )
        self.assertEqual(
            [f["field"] for f in found(after_award, "round_step_forbidden_setting")],
            ["round_scoped"],
        )

    def test_the_description_says_how_a_round_step_completes(self):
        description = wc.Context(
            document([step("administrative_check"), self.meeting(), decision()])
        ).description
        extra = next(s for s in description["steps"] if s["key"] == "panel_meeting")[
            "extra"
        ]
        self.assertEqual(extra["round_completion"], "reports")
        self.assertEqual(extra["round_report_writers"], ["panel_member"])
        plain = description["steps"][0]["extra"]
        self.assertEqual(plain, {"round_scoped": False})


class DomainPanelsTest(TestCase):
    def panel_document(self, panels=None, **chair):
        roles = [
            role("chair", eligible_from="panel:chair", max_holders=1, **chair),
            role("member", eligible_from="panel:member", min_holders=3, max_holders=3),
        ]
        data = document([step("administrative_check"), decision()], roles)
        if panels is not None:
            data["panels"] = panels
        return data

    def test_panel_roles_without_panels(self):
        self.assertEqual(
            len(found(self.panel_document(), "panel_role_without_panels")), 2
        )
        self.assertFalse(
            found(
                self.panel_document([{"key": "a", "name": "A"}]),
                "panel_role_without_panels",
            )
        )

    def test_panel_chair_role_with_many_holders(self):
        self.assertTrue(
            found(self.panel_document(min_holders=2), "panel_chair_role_many_holders")
        )
        self.assertFalse(found(self.panel_document(), "panel_chair_role_many_holders"))

    def test_domains_mapped_to_no_panel(self):
        structure_models.ScienceDomain.objects.all().delete()
        structure_models.ScienceDomain.objects.create(code="1", name="Physics")
        structure_models.ScienceDomain.objects.create(code="2", name="Biology")
        data = self.panel_document(
            [{"key": "a", "name": "A", "science_domains": ["1"]}]
        )
        findings = found(data, "domain_without_panel")
        self.assertEqual(len(findings), 1)
        self.assertIn("Biology", findings[0]["message"])

    def test_a_domain_named_otherwise_here_is_not_covered(self):
        structure_models.ScienceDomain.objects.all().delete()
        structure_models.ScienceDomain.objects.create(code="1", name="Physics")
        covered = self.panel_document(
            [
                {
                    "key": "a",
                    "name": "A",
                    "science_domains": [{"code": "1", "name": "Physics"}],
                }
            ]
        )
        self.assertFalse(found(covered, "domain_without_panel"))
        elsewhere = self.panel_document(
            [
                {
                    "key": "a",
                    "name": "A",
                    "science_domains": [{"code": "1", "name": "Natural sciences"}],
                }
            ]
        )
        self.assertTrue(found(elsewhere, "domain_without_panel"))

    def test_live_panel_rules(self):
        fixture = fixtures.ProposalFixture()
        call = fixture.new_call
        panel = models.CallPanel.objects.create(call=call, key="a", name="A")
        data = self.panel_document([{"key": "a", "name": "A"}])
        self.assertTrue(found(data, "panel_without_chair", call=call))
        self.assertTrue(found(data, "panel_too_small", call=call))
        panel.members.add(*[structure_factories.UserFactory() for _ in range(3)])
        panel.chair = panel.members.first()
        panel.save()
        self.assertFalse(found(data, "panel_without_chair", call=call))
        self.assertFalse(found(data, "panel_too_small", call=call))


def applicant_step(key, phase, **values):
    return step(
        key,
        kind="applicant_action",
        outcome_set="submitted",
        completer="applicant",
        phase=phase,
        **values,
    )


def approval(key, phase, **values):
    return step(key, outcome_set="approval", phase=phase, **values)


class AfterTheAwardTest(SimpleTestCase):
    def test_extension_opens_with_a_request_and_ends_with_a_decision(self):
        data = document(
            [
                step("administrative_check"),
                decision(),
                approval("extension_decision", "extension"),
                applicant_step("extension_request", "extension"),
            ]
        )
        findings = found(data, "extension_structure")
        self.assertEqual(
            {(f["step"], f["field"]) for f in findings},
            {
                ("extension_decision", "kind"),
                ("extension_request", "outcome_set"),
                ("extension_request", "kind"),
            },
        )
        data = document(
            [
                step("administrative_check"),
                decision(),
                applicant_step("extension_request", "extension"),
                approval("extension_decision", "extension"),
            ]
        )
        self.assertFalse(found(data, "extension_structure"))

    def test_closing_opens_with_the_final_report(self):
        data = document(
            [
                step("administrative_check"),
                decision(),
                approval("final_report_approval", "closing"),
            ]
        )
        self.assertTrue(found(data, "closing_structure", "final_report_approval"))
        data = document(
            [
                step("administrative_check"),
                decision(),
                applicant_step("final_report", "closing"),
            ]
        )
        self.assertFalse(found(data, "closing_structure"))
        self.assertFalse(found(data, "no_final_report"))
        self.assertTrue(found(base(), "no_final_report"))

    def test_no_allocation_cancellation_or_condition_after_the_award(self):
        data = document(
            [
                step("administrative_check"),
                decision(),
                applicant_step("extension_request", "extension"),
                approval(
                    "extension_decision",
                    "extension",
                    is_allocation_decision=True,
                    on_negative="cancel",
                    run_condition={
                        "step": "administrative_check",
                        "outcomes": ["eligible"],
                    },
                ),
            ]
        )
        self.assertEqual(
            {f["field"] for f in found(data, "post_award_forbidden_setting")},
            {"is_allocation_decision", "on_negative", "run_condition"},
        )

    def test_inputs_come_from_the_evaluation_or_the_steps_own_phase(self):
        data = document(
            [
                step("administrative_check"),
                decision(),
                applicant_step("extension_request", "extension"),
                approval("extension_decision", "extension"),
                applicant_step("final_report", "closing"),
                approval(
                    "final_report_approval",
                    "closing",
                    input_steps=[
                        "administrative_check",
                        "final_report",
                        "extension_request",
                    ],
                ),
            ]
        )
        findings = found(data, "input_from_other_phase")
        self.assertEqual(len(findings), 1)
        self.assertIn("Extension request", findings[0]["message"])

    def test_a_stage_never_spans_phases(self):
        data = document(
            [
                step("administrative_check"),
                decision(),
                applicant_step(
                    "extension_request", "extension", parallel_with_previous=True
                ),
                approval("extension_decision", "extension"),
            ]
        )
        self.assertTrue(found(data, "stage_spans_phases", "extension_request"))

    def test_an_extension_without_days_before_the_end(self):
        data = document(
            [
                step("administrative_check"),
                decision(),
                applicant_step("extension_request", "extension"),
                approval("extension_decision", "extension"),
            ]
        )
        self.assertTrue(found(data, "extension_without_offset", "extension_request"))
        data["workflow_steps"][2]["project_end_offset_days"] = 30
        self.assertFalse(found(data, "extension_without_offset"))


class BudgetRulesTest(test.APITestCase):
    def test_hard_budget_with_automatic_decision_defers(self):
        data = document(
            [
                step("administrative_check"),
                decision(decides_automatically=True),
            ],
            budget_enforcement="hard",
        )
        self.assertTrue(
            found(data, "automatic_decision_defers_on_budget", "allocation_decision")
        )
        data["call"]["budget_enforcement"] = "soft"
        self.assertFalse(found(data, "automatic_decision_defers_on_budget"))

    def test_live_budget_rules(self):
        fixture = fixtures.ProposalFixture()
        call = fixture.new_call
        call.budget_enforcement = "hard"
        call.save()
        call.round_set.all().delete()
        now = timezone.now()
        first = factories.RoundFactory(
            call=call, start_time=now, cutoff_time=now + timedelta(days=10)
        )
        second = factories.RoundFactory(
            call=call,
            start_time=now + timedelta(days=20),
            cutoff_time=now + timedelta(days=30),
        )
        offering = marketplace_factories.OfferingFactory()
        requested = factories.RequestedOfferingFactory(call=call, offering=offering)
        rule = models.CallOfferingComponent.objects.create(
            requested_offering=requested,
            component_type="cpu",
            is_fixed=True,
            default_amount=20,
        )
        pool = models.CallResourcePool.objects.create(
            offering_component=rule, round=first, offered_total=10
        )
        data = wc.call_document(call)
        uncovered = found(data, "budget_round_uncovered", call=call)
        self.assertEqual(len(uncovered), 1)
        self.assertIn(second.name, uncovered[0]["message"])
        [small] = found(data, "budget_pool_below_request", call=call)
        self.assertIn(first.name, small["message"])
        # A call-wide pool covers every round, and holds a fixed award.
        pool.round = None
        pool.offered_total = 100
        pool.save()
        self.assertFalse(found(data, "budget_round_uncovered", call=call))
        self.assertFalse(found(data, "budget_pool_below_request", call=call))

    def test_round_pool_above_the_whole_call(self):
        call = factories.CallFactory()
        first = factories.RoundFactory(call=call)
        offering = marketplace_factories.OfferingFactory()
        requested = factories.RequestedOfferingFactory(call=call, offering=offering)
        rule = models.CallOfferingComponent.objects.create(
            requested_offering=requested, component_type="storage"
        )
        models.CallResourcePool.objects.create(
            offering_component=rule, round=None, offered_total=23000
        )
        pool = models.CallResourcePool.objects.create(
            offering_component=rule, round=first, offered_total=334343443
        )
        data = wc.call_document(call)
        [above] = found(data, "budget_round_above_call", call=call)
        self.assertIn("334,343,443", above["message"])
        self.assertIn("23,000", above["message"])
        # Up to the whole call's pool is fine; so is a round without a cap.
        pool.offered_total = 23000
        pool.save()
        self.assertFalse(found(data, "budget_round_above_call", call=call))


class ShippedTemplatesTest(TestCase):
    def test_the_tenders_steps_after_the_award(self):
        """Extreme Scale, Regular Access and AI for Science, and of the other
        presets the two-stage review and the semi-annual panel, take extension
        requests up to 30 days before the project's end; every template asks
        for a final report."""
        for name in call_templates.names():
            with self.subTest(template=name):
                description = wc.Context(call_templates.load(name)).description
                phases = description["call"]["extra"]["phases"]
                self.assertIn("closing", phases)
                if name in (
                    "extreme_scale",
                    "regular_access",
                    "ai_for_science",
                    "technical_and_scientific_review",
                    "access_maximize",
                ):
                    self.assertEqual(phases["extension"]["project_end_offset_days"], 30)
                else:
                    self.assertNotIn("extension", phases)

    def test_no_shipped_template_has_structural_errors(self):
        for name in call_templates.names():
            with self.subTest(template=name):
                findings = wc.check(call_templates.load(name))
                self.assertEqual(wc.errors(findings), [])
                for finding in findings:
                    if finding["severity"] == wc.WARNING:
                        print(  # noqa: T201
                            f"{name}: {finding['code']} {finding['step']}: "
                            f"{finding['message']}"
                        )


class LiveRulesTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.new_call

    def codes(self):
        return {f["code"] for f in wc.check_call(self.call)}

    def test_round_and_offerings(self):
        codes = self.codes()
        self.assertIn("no_round", codes)
        self.assertIn("no_accepted_offering", codes)
        factories.RoundFactory(call=self.call)
        offering = factories.RequestedOfferingFactory(
            call=self.call, state=RequestedOfferingStates.ACCEPTED, plan=None
        )
        codes = self.codes()
        self.assertNotIn("no_round", codes)
        self.assertNotIn("no_accepted_offering", codes)
        planless = [
            f for f in wc.check_call(self.call) if f["code"] == "offering_without_plan"
        ]
        self.assertIn(offering.offering.name, planless[0]["message"])

    def test_panel_chair_completer_without_chair(self):
        step = self.call.workflow_steps.filter(is_enabled=True).first()
        step.completer = "panel_chair"
        step.save()
        self.assertIn("completer_unholdable", self.codes())
        self.call.panel_chair = self.fixture.panel_member
        self.call.save()
        self.assertNotIn("completer_unholdable", self.codes())

    def test_empty_reviewer_pool(self):
        step = self.call.workflow_steps.filter(kind="assessment").first()
        step.contributor_source = "assigned_reviewers"
        step.is_enabled = True
        step.save()
        self.assertIn("empty_reviewer_pool", self.codes())


class ActivationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.new_call

    def activate(self):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(
            factories.CallFactory.get_protected_url(self.call, "activate")
        )

    def test_all_errors_come_at_once(self):
        response = self.activate()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        codes = {f["code"] for f in response.data["workflow_check"]}
        self.assertTrue({"no_round", "no_accepted_offering"} <= codes)
        self.assertIn(
            "Call must have a round to be activated.", response.data["detail"]
        )
        self.assertIn(
            "Call must have at least one accepted offering to be activated.",
            response.data["detail"],
        )
        self.call.refresh_from_db()
        self.assertEqual(self.call.state, CallStates.DRAFT)

    def test_warnings_do_not_block(self):
        factories.RoundFactory(call=self.call)
        factories.RequestedOfferingFactory(
            call=self.call, state=RequestedOfferingStates.ACCEPTED
        )
        step = (
            self.call.workflow_steps.filter(is_enabled=True)
            .exclude(is_allocation_decision=True)
            .first()
        )
        step.duration_in_days = None
        step.save()
        findings = wc.check_call(self.call)
        self.assertTrue(any(f["severity"] == wc.WARNING for f in findings))
        self.assertEqual(wc.errors(findings), [])
        response = self.activate()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class CheckEndpointTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.url = factories.CallFactory.get_protected_url(self.call, "workflow-check")

    def get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.url)

    def test_call_team_staff_and_support_read_it(self):
        for user in (
            self.fixture.call_manager,
            self.fixture.call_organizer_user,
            self.fixture.staff,
            structure_factories.UserFactory(is_support=True),
        ):
            with self.subTest(user=user):
                response = self.get(user)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertIn("can_activate", response.data)

    def test_panel_reviewers_and_applicants_do_not(self):
        for user in (
            self.fixture.panel_member,
            self.fixture.reviewer_1,
            self.fixture.proposal_creator,
            structure_factories.UserFactory(),
        ):
            with self.subTest(user=user):
                self.assertEqual(self.get(user).status_code, status.HTTP_404_NOT_FOUND)

    def test_template_check(self):
        template = models.CallTemplate.objects.create(
            name="Template",
            document=call_templates.load("extreme_scale"),
            schema_version=3,
        )
        url = "http://testserver" + reverse(
            "call-template-workflow-check", kwargs={"uuid": template.uuid.hex}
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["can_activate"])
        self.client.force_authenticate(self.fixture.call_organizer_user)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)
        template.is_published = True
        template.save()
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)


class ImportedTemplatesAreCleanTest(test.APITestCase):
    def test_a_call_from_each_template_has_no_structural_errors(self):
        manager = factories.CallManagingOrganisationFactory()
        user = structure_factories.UserFactory(is_staff=True)
        for name in call_templates.names():
            with self.subTest(template=name):
                call, _, _ = call_transfer.import_call(
                    copy.deepcopy(call_templates.load(name)), manager, user
                )
                live_only = {r.code for r in wc.RULES if r.live}
                errors = [
                    f
                    for f in wc.errors(wc.check_call(call))
                    if f["code"] not in live_only
                    and f["code"] != "completer_unholdable"
                ]
                self.assertEqual(errors, [])
