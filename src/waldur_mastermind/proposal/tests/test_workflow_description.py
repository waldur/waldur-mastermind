"""The workflow description: one reading of a call's workflow for every view."""

import ast
import copy
import datetime
import json
from pathlib import Path

from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.structure.presets import load_preset
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_templates,
    call_transfer,
    models,
    tasks,
)
from waldur_mastermind.proposal import workflow_description as wd
from waldur_mastermind.proposal.tests import factories, fixtures

TESTS = Path(__file__).resolve().parent


def step_of(description, key):
    return next(s for s in description["steps"] if s["key"] == key)


def cell(step, actor):
    return next(c for c in step["roles"] if c["actor"] == actor)


def fields(flag_cell):
    return {b["field"] for b in flag_cell["because"]}


def codes(description, step=None):
    return [
        f["code"]
        for f in description["notifications"]["flags"]
        if step is None or f["step"] == step
    ]


def rule(trigger, recipient, days_before=None, proposal_role=None, enabled=True):
    return {
        "trigger": trigger,
        "recipient": recipient,
        "days_before": days_before,
        "is_enabled": enabled,
        "proposal_role": proposal_role,
        "notified_proposal_roles": [],
    }


def document(steps, roles=(), **call):
    return {
        "schema_version": 3,
        "call": {"name": "Call", **call},
        "proposal_roles": list(roles),
        "workflow_steps": [{"order": index, **s} for index, s in enumerate(steps)],
    }


class TemplatesAndCallsTest(test.APITestCase):
    def setUp(self):
        # The Regular Access panels route the domains of this preset.
        load_preset("cscs")

    def test_a_call_is_described_as_the_template_it_was_created_from(self):
        manager = factories.CallManagingOrganisationFactory()
        user = structure_factories.UserFactory(is_staff=True)
        for name in call_templates.names():
            with self.subTest(template=name):
                template = call_templates.load(name)
                call, _, _ = call_transfer.import_call(
                    copy.deepcopy(template), manager, user
                )
                exported, _ = call_transfer.export_call(call)
                self.assertEqual(wd.describe(template), wd.describe(exported))

    def test_every_shipped_template_is_described(self):
        for name in call_templates.names():
            with self.subTest(template=name):
                description = wd.describe(call_templates.load(name))
                self.assertEqual(description["schema"], wd.SCHEMA)
                self.assertTrue(description["steps"])
                for step in description["steps"]:
                    self.assertEqual(
                        [c["actor"] for c in step["roles"]],
                        [a["key"] for a in description["actors"]],
                    )


class RoleCellsTest(SimpleTestCase):
    def setUp(self):
        self.description = wd.describe(call_templates.load("extreme_scale"))

    def test_actors_include_evaluator_roles_and_their_lead(self):
        keys = [a["key"] for a in self.description["actors"]]
        self.assertEqual(
            keys[:6],
            [
                "applicant",
                "call_manager",
                "panel_chair",
                "panel_member",
                "reviewer",
                "offering_team",
            ],
        )
        self.assertIn("proposal_role:rapporteur", keys)
        self.assertIn("proposal_role_lead:rapporteur", keys)
        self.assertEqual(keys[-1], "system")

    def test_lead_only_step_is_completed_by_the_lead(self):
        step = step_of(self.description, "consolidated_report")
        self.assertTrue(step["completion_lead_only"])
        lead = cell(step, "proposal_role_lead:rapporteur")
        self.assertTrue(lead["completes"]["value"])
        self.assertIn("contributors_lead_only", fields(lead["completes"]))
        self.assertFalse(cell(step, "proposal_role:rapporteur")["completes"]["value"])

    def test_the_panel_chair_completes_a_chair_step(self):
        step = step_of(self.description, "rapporteur_assignment")
        self.assertTrue(cell(step, "panel_chair")["completes"]["value"])
        self.assertEqual(step["assigns_roles"], ["rapporteur"])

    def test_blind_review_keeps_reviewers_from_each_others_work(self):
        step = step_of(self.description, "scientific_review")
        reviewer = cell(step, "reviewer")
        self.assertTrue(reviewer["contributes"]["value"])
        self.assertFalse(reviewer["reads_outputs"]["value"])
        self.assertIn("blind_review", fields(reviewer["reads_outputs"]))

    def test_later_steps_read_their_inputs(self):
        step = step_of(self.description, "scientific_review")
        reads = cell(step, "applicant")["reads_outputs"]
        self.assertTrue(reads["value"])
        self.assertIn(
            {
                "source": "step",
                "step": "response_to_reviews",
                "field": "input_steps",
                "detail": None,
            },
            reads["because"],
        )

    def test_a_released_report_is_read_by_the_applicant(self):
        step = step_of(self.description, "consolidated_report")
        self.assertIn(
            "release_to_applicant", fields(cell(step, "applicant")["reads_outputs"])
        )

    def test_delegation_lets_the_offering_team_contribute(self):
        step = step_of(self.description, "technical_assessment")
        team = cell(step, "offering_team")
        self.assertIn("delegable_to", fields(team["contributes"]))
        self.assertEqual(
            step["delegable_to"], {"target": "offering_team", "proposal_role": None}
        )

    def test_parallel_stage_and_schedule(self):
        technical = step_of(self.description, "technical_assessment")
        assignment = step_of(self.description, "rapporteur_assignment")
        self.assertEqual(technical["stage"], assignment["stage"])
        stage = self.description["schedule"]["stages"][technical["stage"]]
        # The stage lasts as long as its longest step.
        self.assertEqual(stage["end_day"] - stage["start_day"], 21)
        self.assertIn(
            "technical_assessment", self.description["schedule"]["critical_path"]
        )

    def test_notified_cells_carry_their_triggers(self):
        step = step_of(self.description, "award_response")
        notified = cell(step, "applicant")["notified"]
        self.assertTrue(notified["value"])
        self.assertIn(
            {"trigger": "step_started", "days_before": None}, notified["triggers"]
        )
        self.assertTrue(cell(step, "offering_team")["notified"]["value"])

    def test_reviews_visible_to_submitters_lets_the_applicant_read_review_outcomes(
        self,
    ):
        doc = call_templates.load("extreme_scale")
        doc["call"]["reviews_visible_to_submitters"] = True
        step = step_of(wd.describe(doc), "scientific_review")
        self.assertIn(
            "reviews_visible_to_submitters",
            fields(cell(step, "applicant")["reads_outputs"]),
        )

    def test_automatic_decision_is_completed_by_the_system(self):
        doc = document(
            [
                {
                    "key": "decide",
                    "kind": "decision",
                    "outcome_set": "approval",
                    "completer": "call_manager",
                    "is_allocation_decision": True,
                    "decides_automatically": True,
                }
            ]
        )
        step = step_of(wd.describe(doc), "decide")
        self.assertTrue(step["decides_automatically"])
        self.assertEqual(
            fields(cell(step, "system")["completes"]), {"decides_automatically"}
        )
        self.assertEqual(step["automatic_decision_guard"], "earlier_step")

    def test_automatic_decision_tells_what_guards_it(self):
        decide = {
            "key": "decide",
            "kind": "decision",
            "outcome_set": "approval",
            "completer": "call_manager",
            "is_allocation_decision": True,
        }
        step = step_of(wd.describe(document([decide])), "decide")
        self.assertIsNone(step["automatic_decision_guard"])
        decide.update(
            decides_automatically=True,
            automatic_decision_guard="provider_fulfilment",
        )
        step = step_of(wd.describe(document([decide])), "decide")
        self.assertEqual(step["automatic_decision_guard"], "provider_fulfilment")

    def test_conditions_and_change_requests_are_described(self):
        doc = document(
            [
                {
                    "key": "tech",
                    "kind": "assessment",
                    "outcome_set": "feasibility",
                    "completer": "offering_manager",
                },
                {
                    "key": "extra",
                    "kind": "assessment",
                    "outcome_set": "done",
                    "completer": "call_manager",
                    "run_condition": {"step": "tech", "outcomes": ["infeasible"]},
                    "allows_change_requests": True,
                },
            ]
        )
        step = step_of(wd.describe(doc), "extra")
        self.assertEqual(
            step["run_condition"],
            {"step": "tech", "outcomes": ["infeasible"], "score_spread_above": None},
        )
        self.assertTrue(step["allows_change_requests"])

    def test_applicant_labels_match_the_shared_fixture(self):
        cases = json.loads((TESTS / "applicant_labels.json").read_text())["cases"]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(
                    wd.applicant_label(case["step"]),
                    (case["label_kind"], case["label"]),
                )

    def test_configured_applicant_labels_are_described_where_they_reach(self):
        labels = {
            "applicant": {"active": "Under check", "outcomes": {"eligible": "OK"}},
            "evaluators": {"active": "Pending check"},
        }
        doc = document(
            [
                {
                    "key": "admin",
                    "kind": "decision",
                    "outcome_set": "eligibility",
                    "applicant_visible": True,
                    "status_labels": labels,
                },
                {
                    "key": "tech",
                    "kind": "assessment",
                    "outcome_set": "feasibility",
                    "status_labels": labels,
                },
            ]
        )
        described = wd.describe(doc)
        admin = step_of(described, "admin")
        self.assertEqual(admin["applicant"]["label_kind"], "configured")
        self.assertEqual(admin["applicant"]["label"], "Under check")
        self.assertEqual(admin["applicant"]["labels"], labels["applicant"])
        self.assertEqual(admin["status_labels"], labels)
        tech = step_of(described, "tech")
        self.assertEqual(tech["applicant"]["label_kind"], "internal_check")
        self.assertEqual(tech["applicant"]["labels"], {})
        self.assertIn("applicant_labels_on_hidden_step", codes(described, "tech"))
        self.assertNotIn("applicant_labels_on_hidden_step", codes(described, "admin"))


class LaterFeaturesTest(SimpleTestCase):
    """Round steps, domain panels, budgets and the steps after the award."""

    def gate(self, key="administrative_check", **values):
        return {
            "key": key,
            "name": key.replace("_", " ").capitalize(),
            "kind": "decision",
            "outcome_set": "eligibility",
            "completer": "call_manager",
            "duration_in_days": 7,
            **values,
        }

    def decision(self):
        return self.gate(
            "allocation_decision",
            outcome_set="approval",
            is_allocation_decision=True,
            duration_in_days=5,
        )

    def test_a_round_step_says_who_writes_its_reports_and_how_it_completes(self):
        meeting = self.gate(
            "panel_meeting",
            kind="assessment",
            outcome_set="done",
            round_scoped=True,
            contributor_source="call_role:panel_chair",
        )
        description = wd.describe(
            document(
                [self.gate(), meeting, self.decision()], evaluation_start="at_cutoff"
            )
        )
        extra = step_of(description, "panel_meeting")["extra"]
        self.assertTrue(extra["round_scoped"])
        self.assertEqual(extra["round_report_writers"], ["panel_chair"])
        self.assertEqual(extra["round_completion"], "reports")
        self.assertFalse(
            step_of(description, "administrative_check")["extra"]["round_scoped"]
        )

    def test_panel_roles_are_marked_and_the_panels_listed(self):
        roles = [
            {
                "key": "domain_panel_chair",
                "name": "Domain Panel Chair",
                "eligible_from": "panel:chair",
                "min_holders": 1,
                "max_holders": 1,
            }
        ]
        assignment = self.gate(
            "chair_assignment",
            kind="assignment",
            outcome_set="done",
            assigns_roles=["domain_panel_chair"],
        )
        data = document([self.gate(), assignment, self.decision()], roles)
        data["panels"] = [
            {"key": "physics", "name": "Physics", "science_domains": ["1", "2"]}
        ]
        description = wd.describe(data)
        actors = {a["key"]: a for a in description["actors"]}
        self.assertEqual(
            actors["proposal_role:domain_panel_chair"]["notes"],
            ["held_by_chair_of_proposal_panel"],
        )
        self.assertEqual(
            step_of(description, "chair_assignment")["extra"]["panel_roles"],
            [{"role": "domain_panel_chair", "eligible_from": "panel:chair"}],
        )
        extra = description["call"]["extra"]
        self.assertEqual(
            extra["panels"],
            [
                {
                    "key": "physics",
                    "name": "Physics",
                    "science_domains": 2,
                    "science_sub_domains": 0,
                }
            ],
        )
        self.assertEqual(extra["panel_roles"], ["domain_panel_chair"])

    def test_a_hard_budget_allocates_first_come_first_served(self):
        description = wd.describe(
            document([self.gate(), self.decision()], budget_enforcement="hard")
        )
        self.assertEqual(description["call"]["settings"]["budget_enforcement"], "hard")
        self.assertTrue(description["call"]["extra"]["first_come_first_served"])
        soft = wd.describe(document([self.gate(), self.decision()]))
        self.assertEqual(soft["call"]["extra"]["budget_enforcement"], "soft")
        self.assertFalse(soft["call"]["extra"]["first_come_first_served"])

    def test_the_steps_after_the_award_are_planned_on_their_own(self):
        request = self.gate(
            "extension_request",
            kind="applicant_action",
            outcome_set="submitted",
            completer="applicant",
            duration_in_days=14,
            phase="extension",
            project_end_offset_days=30,
        )
        extension_decision = self.gate(
            "extension_decision",
            outcome_set="approval",
            duration_in_days=10,
            phase="extension",
        )
        report = self.gate(
            "final_report",
            kind="applicant_action",
            outcome_set="submitted",
            completer="applicant",
            duration_in_days=30,
            phase="closing",
            project_end_offset_days=0,
        )
        description = wd.describe(
            document(
                [self.gate(), self.decision(), request, extension_decision, report]
            )
        )
        schedule = description["schedule"]
        self.assertEqual(
            [s["key"] for s in schedule["steps"]],
            ["administrative_check", "allocation_decision"],
        )
        self.assertEqual(schedule["total_days"], 12)
        extension, closing = schedule["post_award"]
        self.assertEqual(extension["phase"], "extension")
        self.assertEqual(extension["project_end_offset_days"], 30)
        self.assertEqual(
            [(s["key"], s["start_day"]) for s in extension["steps"]],
            [("extension_request", 0), ("extension_decision", 14)],
        )
        self.assertEqual(extension["total_days"], 24)
        self.assertEqual(closing["phase"], "closing")
        self.assertEqual(closing["project_end_offset_days"], 0)
        self.assertEqual(
            step_of(description, "extension_request")["phase"], "extension"
        )
        self.assertEqual(
            description["call"]["extra"]["phases"],
            {
                "extension": {
                    "steps": ["extension_request", "extension_decision"],
                    "project_end_offset_days": 30,
                    "opens_with": "extension_request",
                    "ends_with": "extension_decision",
                },
                "closing": {
                    "steps": ["final_report"],
                    "project_end_offset_days": 0,
                    "opens_with": "final_report",
                    "ends_with": "final_report",
                },
            },
        )


class TimeToDecisionTest(SimpleTestCase):
    def test_allocation_date_wins_over_the_review_duration(self):
        cutoff = timezone.now()
        doc = document([], rounds=None)
        doc["rounds"] = [
            {
                "start_time": (cutoff - datetime.timedelta(days=30)).isoformat(),
                "cutoff_time": cutoff.isoformat(),
                "allocation_date": (cutoff + datetime.timedelta(days=60)).isoformat(),
                "review_duration_in_days": 10,
            },
            {
                "start_time": cutoff.isoformat(),
                "cutoff_time": None,
                "allocation_date": None,
                "review_duration_in_days": 10,
            },
        ]
        rounds = wd.describe(doc)["call"]["rounds"]
        self.assertEqual(
            (rounds[0]["time_to_decision_days"], rounds[0]["time_to_decision_source"]),
            (60, "allocation_date"),
        )
        self.assertEqual(
            (rounds[1]["time_to_decision_days"], rounds[1]["time_to_decision_source"]),
            (10, "review_duration_in_days"),
        )
        self.assertTrue(rounds[1]["open_ended"])

    def test_a_target_wins_over_the_review_duration_when_present(self):
        doc = document([], target_decision_days=45)
        doc["rounds"] = [
            {
                "start_time": timezone.now().isoformat(),
                "cutoff_time": None,
                "review_duration_in_days": 10,
            }
        ]
        self.assertEqual(
            wd.describe(doc)["call"]["rounds"][0]["time_to_decision_days"], 45
        )


class NotificationFlagsTest(SimpleTestCase):
    def test_applicant_step_without_a_start_mail_is_missing_one(self):
        doc = document(
            [
                {
                    "key": "respond",
                    "kind": "applicant_action",
                    "outcome_set": "submitted",
                    "completer": "applicant",
                    "applicant_visible": True,
                }
            ]
        )
        self.assertIn(
            "applicant_step_not_announced", codes(wd.describe(doc), "respond")
        )

    def test_decision_without_a_rule_to_the_applicant(self):
        doc = document(
            [
                {
                    "key": "decide",
                    "kind": "decision",
                    "outcome_set": "approval",
                    "completer": "call_manager",
                    "is_allocation_decision": True,
                }
            ]
        )
        self.assertIn(
            "decision_not_announced_to_team", codes(wd.describe(doc), "decide")
        )
        doc["workflow_steps"][0]["notification_rules"] = [
            rule("step_completed", "applicant")
        ]
        self.assertNotIn("decision_not_announced_to_team", codes(wd.describe(doc)))

    def test_step_with_a_deadline_and_no_reminder(self):
        doc = document(
            [
                {
                    "key": "check",
                    "kind": "decision",
                    "outcome_set": "eligibility",
                    "completer": "call_manager",
                    "duration_in_days": 7,
                }
            ]
        )
        self.assertIn("no_deadline_reminder", codes(wd.describe(doc), "check"))
        doc["workflow_steps"][0]["notification_rules"] = [
            rule("deadline_approaching", "call_managers", 1)
        ]
        self.assertNotIn("no_deadline_reminder", codes(wd.describe(doc)))

    def test_two_rules_for_one_audience_are_a_duplicate(self):
        doc = document(
            [
                {
                    "key": "review",
                    "kind": "assessment",
                    "outcome_set": "done",
                    "completer": "call_manager",
                    "notification_rules": [
                        rule("step_started", "assigned_reviewers"),
                        rule("step_started", "all_pool_reviewers"),
                    ],
                }
            ]
        )
        self.assertIn("duplicate_rule", codes(wd.describe(doc), "review"))

    def test_the_responsible_role_beside_its_own_audience_is_no_duplicate(self):
        # Every step's default rules: the completer and the call managers,
        # on a step the call managers complete.
        doc = document(
            [
                {
                    "key": "check",
                    "kind": "decision",
                    "outcome_set": "eligibility",
                    "completer": "call_manager",
                    "notification_rules": [
                        rule("step_expired", "responsible_role"),
                        rule("step_expired", "call_managers"),
                    ],
                }
            ]
        )
        self.assertNotIn("duplicate_rule", codes(wd.describe(doc), "check"))

    def test_completed_then_started_to_the_applicant_is_two_mails(self):
        doc = document(
            [
                {
                    "key": "a",
                    "kind": "assessment",
                    "outcome_set": "done",
                    "completer": "call_manager",
                    "applicant_visible": True,
                    "notification_rules": [rule("step_completed", "applicant")],
                },
                {
                    "key": "b",
                    "kind": "applicant_action",
                    "outcome_set": "submitted",
                    "completer": "applicant",
                    "applicant_visible": True,
                    "notification_rules": [rule("step_started", "applicant")],
                },
            ]
        )
        self.assertIn("two_mails_one_transition", codes(wd.describe(doc), "b"))

    def test_no_two_mails_across_phases(self):
        # An extension's decision never starts the final report: each phase
        # runs on its own.
        doc = document(
            [
                {
                    "key": "a",
                    "kind": "decision",
                    "outcome_set": "done",
                    "completer": "call_manager",
                    "phase": "extension",
                    "applicant_visible": True,
                    "notification_rules": [rule("step_completed", "applicant")],
                },
                {
                    "key": "b",
                    "kind": "applicant_action",
                    "outcome_set": "submitted",
                    "completer": "applicant",
                    "phase": "closing",
                    "applicant_visible": True,
                    "notification_rules": [rule("step_started", "applicant")],
                },
            ]
        )
        self.assertNotIn("two_mails_one_transition", codes(wd.describe(doc), "b"))

    def test_extension_request_needs_no_start_mail(self):
        # The applicant opens an extension run by asking for one.
        steps = [
            {
                "key": key,
                "kind": "applicant_action",
                "outcome_set": "submitted",
                "completer": "applicant",
                "phase": "extension",
                "applicant_visible": True,
            }
            for key in ("ask", "follow_up")
        ]
        described = wd.describe(document(steps))
        self.assertNotIn("applicant_step_not_announced", codes(described, "ask"))
        self.assertIn("applicant_step_not_announced", codes(described, "follow_up"))

    def test_rules_resolving_to_nobody(self):
        roles = [{"key": "rapporteur", "name": "Rapporteur", "allows_lead": False}]
        doc = document(
            [
                {
                    "key": "a",
                    "kind": "assessment",
                    "outcome_set": "done",
                    "completer": "call_manager",
                    "notification_rules": [
                        rule(
                            "step_started", "proposal_role", proposal_role="rapporteur"
                        ),
                        rule(
                            "step_completed",
                            "proposal_role_lead",
                            proposal_role="rapporteur",
                        ),
                        rule("step_started", "panel_chair"),
                    ],
                }
            ],
            roles=roles,
        )
        found = codes(wd.describe(doc), "a")
        self.assertIn("role_not_assigned_before", found)
        self.assertIn("role_without_lead", found)
        # Without live counts nothing is known about the chair.
        self.assertNotIn("no_panel_chair", found)
        live = wd.LiveContext(
            holders={"panel_chair": 0},
            requested_offerings=1,
            offerings_without_managers=1,
        )
        self.assertIn("no_panel_chair", codes(wd.describe(doc, live), "a"))

    def test_role_working_the_step_is_reachable(self):
        # The call managers assign the role by hand; the step draws its
        # contributors from it, so a reminder to the role reaches them.
        roles = [{"key": "consensus_expert", "name": "Consensus expert"}]
        doc = document(
            [
                {
                    "key": "consensus",
                    "kind": "assessment",
                    "outcome_set": "done",
                    "completer": "call_manager",
                    "contributor_source": "proposal_role:consensus_expert",
                    "notification_rules": [
                        rule(
                            "deadline_approaching",
                            "proposal_role",
                            days_before=2,
                            proposal_role="consensus_expert",
                        )
                    ],
                }
            ],
            roles=roles,
        )
        self.assertNotIn(
            "role_not_assigned_before", codes(wd.describe(doc), "consensus")
        )

    def test_role_assigned_by_an_earlier_step_is_reachable(self):
        roles = [{"key": "rapporteur", "name": "Rapporteur"}]
        doc = document(
            [
                {
                    "key": "assign",
                    "kind": "assignment",
                    "outcome_set": "done",
                    "completer": "call_manager",
                    "assigns_roles": ["rapporteur"],
                },
                {
                    "key": "a",
                    "kind": "assessment",
                    "outcome_set": "done",
                    "completer": "call_manager",
                    "notification_rules": [
                        rule(
                            "step_started", "proposal_role", proposal_role="rapporteur"
                        )
                    ],
                },
            ],
            roles=roles,
        )
        self.assertNotIn("role_not_assigned_before", codes(wd.describe(doc)))

    def test_offering_teams_without_managers(self):
        doc = document(
            [
                {
                    "key": "a",
                    "kind": "assessment",
                    "outcome_set": "done",
                    "completer": "call_manager",
                    "notification_rules": [rule("step_completed", "offering_teams")],
                }
            ]
        )
        live = wd.LiveContext(
            holders={}, requested_offerings=2, offerings_without_managers=1
        )
        self.assertIn("offering_without_managers", codes(wd.describe(doc, live), "a"))

    def test_mail_to_the_applicant_naming_an_internal_step_is_a_leak_risk(self):
        doc = document(
            [
                {
                    "key": "a",
                    "kind": "assessment",
                    "outcome_set": "done",
                    "completer": "call_manager",
                    "notification_rules": [rule("step_completed", "applicant")],
                }
            ]
        )
        self.assertIn("internal_step_named_to_applicant", codes(wd.describe(doc), "a"))
        doc["workflow_steps"][0]["applicant_visible"] = True
        self.assertNotIn("internal_step_named_to_applicant", codes(wd.describe(doc)))

    def test_disabled_rules_are_listed_and_do_not_notify(self):
        doc = document(
            [
                {
                    "key": "a",
                    "kind": "assessment",
                    "outcome_set": "done",
                    "completer": "call_manager",
                    "notification_rules": [
                        rule("step_completed", "call_managers", enabled=False)
                    ],
                }
            ]
        )
        step = step_of(wd.describe(doc), "a")
        self.assertEqual([n["enabled"] for n in step["notifications"]], [False])
        self.assertFalse(cell(step, "call_manager")["notified"]["value"])

    def test_every_rule_independent_mail_is_listed(self):
        source = Path(tasks.__file__).read_text()
        names = {
            node.name
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef)
            and node.name.lstrip("_").startswith(("notify", "send"))
        }
        listed = {task for task, _, _ in wd.ALWAYS_SENT}
        self.assertEqual(
            names - listed - wd.RULE_DRIVEN_TASKS - wd.DISPATCHING_TASKS, set()
        )
        self.assertEqual(listed - names, set())


class DescriptionEndpointTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.url = factories.CallFactory.get_protected_url(
            self.call, "workflow-description"
        )

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
                self.assertEqual(self.get(user).status_code, status.HTTP_200_OK)

    def test_panel_reviewers_and_applicants_do_not(self):
        for user in (
            self.fixture.panel_member,
            self.fixture.reviewer_1,
            self.fixture.proposal_creator,
            structure_factories.UserFactory(),
        ):
            with self.subTest(user=user):
                self.assertEqual(self.get(user).status_code, status.HTTP_404_NOT_FOUND)

    def test_live_context_names_nobody(self):
        manager = self.fixture.call_manager
        panel = self.fixture.panel_member
        self.call.panel_chair = panel
        self.call.save()
        response = self.get(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        text = json.dumps(response.data, default=str)
        for user in (manager, panel, self.fixture.reviewer_1):
            for value in (user.username, user.email, user.full_name, user.uuid.hex):
                if value:
                    self.assertNotIn(value, text)
        actors = {a["key"]: a for a in response.data["actors"]}
        self.assertEqual(actors["panel_chair"]["holders"], 1)
        self.assertGreaterEqual(actors["call_manager"]["holders"], 1)

    def test_template_description(self):
        template = models.CallTemplate.objects.create(
            name="Template",
            document=call_templates.load("extreme_scale"),
            schema_version=3,
            is_published=False,
        )
        url = "http://testserver" + reverse(
            "call-template-workflow-description", kwargs={"uuid": template.uuid.hex}
        )
        self.client.force_authenticate(self.fixture.staff)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)
        creator = self.fixture.call_organizer_user
        self.client.force_authenticate(creator)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)
        template.is_published = True
        template.save()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        steps = response.data["steps"]
        self.assertEqual(len(steps), 18)
        self.assertEqual(
            len([s for s in steps if s["phase"] == "evaluation"]),
            13,
        )
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)
