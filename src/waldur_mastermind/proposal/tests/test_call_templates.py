"""The call templates shipped with the call module import cleanly.

Each template is imported through the API's import_call action, as a call
manager would, so a schema change that breaks one fails here first.
"""

import decimal
import re

from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist.enums import QuestionTypes
from waldur_core.structure.models import ScienceSubDomain
from waldur_core.structure.presets import load_preset
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_templates,
    call_transfer,
    models,
    panels,
    workflow_checks,
    workflow_description,
)
from waldur_mastermind.proposal.enums import (
    AllocationTimes,
    CallStates,
    ContributorSources,
    EvaluationStart,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    OnNegativeOutcomes,
    OutcomeSets,
    ProposalRoleEligibility,
    ResponsibleRoles,
    ResultsPublication,
    StepKinds,
    WorkflowPhases,
)
from waldur_mastermind.proposal.tests import factories

EXTREME_SCALE_STEPS = [
    ("administrative_check", StepKinds.DECISION, OutcomeSets.ELIGIBILITY),
    ("technical_assessment", StepKinds.ASSESSMENT, OutcomeSets.PER_OFFERING),
    ("rapporteur_assignment", StepKinds.ASSIGNMENT, OutcomeSets.DONE),
    ("rapporteur_questions", StepKinds.ASSESSMENT, OutcomeSets.DONE),
    ("reviewer_assignment", StepKinds.ASSIGNMENT, OutcomeSets.DONE),
    ("scientific_review", StepKinds.ASSESSMENT, OutcomeSets.REVIEWED),
    ("response_to_reviews", StepKinds.APPLICANT_ACTION, OutcomeSets.SUBMITTED),
    ("individual_reports", StepKinds.ASSESSMENT, OutcomeSets.DONE),
    ("consolidated_report", StepKinds.ASSESSMENT, OutcomeSets.DONE),
    ("committee_meeting", StepKinds.ASSESSMENT, OutcomeSets.DONE),
    ("final_feedback", StepKinds.ASSESSMENT, OutcomeSets.DONE),
    ("allocation_decision", StepKinds.DECISION, OutcomeSets.APPROVAL),
    ("award_response", StepKinds.APPLICANT_ACTION, OutcomeSets.ACCEPTANCE),
    ("extension_request", StepKinds.APPLICANT_ACTION, OutcomeSets.SUBMITTED),
    ("extension_assessment", StepKinds.ASSESSMENT, OutcomeSets.PER_OFFERING),
    ("extension_decision", StepKinds.DECISION, OutcomeSets.APPROVAL),
    ("final_report", StepKinds.APPLICANT_ACTION, OutcomeSets.SUBMITTED),
    ("final_report_approval", StepKinds.DECISION, OutcomeSets.APPROVAL),
]


class CallTemplateImportTest(test.APITestCase):
    def setUp(self):
        # The Regular Access panels cover the domains of this preset.
        load_preset("cscs")
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.manager = factories.CallManagingOrganisationFactory()

    def import_template(self, name):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_list_url("import_call"),
            {
                "manager": self.manager.uuid.hex,
                "call_data": call_templates.load(name),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response

    def test_every_template_imports_without_warnings(self):
        self.assertIn("extreme_scale", call_templates.names())
        for name in call_templates.names():
            with self.subTest(template=name):
                response = self.import_template(name)
                self.assertEqual(response.data["warnings"], [])
                self.assertEqual(response.data["imported_sections"], ["workflow_steps"])

    def test_every_template_has_a_final_report_and_the_tenders_extensions(self):
        # The tender gives Extreme Scale, Regular Access and AI for Science
        # an extension request; every access call a final report. Of the
        # other presets, the two-stage review and the semi-annual panel take
        # extension requests too.
        with_extension = {
            "extreme_scale",
            "regular_access",
            "ai_for_science",
            "technical_and_scientific_review",
            "access_maximize",
        }
        for name in call_templates.names():
            with self.subTest(template=name):
                call = models.Call.objects.get(
                    uuid=self.import_template(name).data["call_uuid"]
                )
                phases = set(call.workflow_steps.values_list("phase", flat=True))
                self.assertIn(WorkflowPhases.CLOSING, phases)
                self.assertEqual(
                    WorkflowPhases.EXTENSION in phases, name in with_extension
                )
                self.assertEqual(
                    models.CallWorkflowStep.phase_structure_errors(call.pk), []
                )

    def test_every_template_survives_a_round_trip(self):
        # Whatever a template sets is what the imported call exports: a field
        # renamed or dropped from the models shows up here, not as a template
        # that silently configures less than it says.
        for name in call_templates.names():
            with self.subTest(template=name):
                template = call_templates.load(name)
                call = models.Call.objects.get(
                    uuid=self.import_template(name).data["call_uuid"]
                )
                exported, warnings = call_transfer.export_call(call)
                self.assertEqual(warnings, [])
                self.assert_contains(exported["call"], template["call"], "call")
                self.assertEqual(
                    len(exported["proposal_roles"]), len(template["proposal_roles"])
                )
                for index, role in enumerate(template["proposal_roles"]):
                    self.assert_contains(
                        exported["proposal_roles"][index],
                        role,
                        f"proposal_roles[{index}]",
                    )
                self.assertEqual(
                    [step["key"] for step in exported["workflow_steps"]],
                    [step["key"] for step in template["workflow_steps"]],
                )
                for index, step in enumerate(template["workflow_steps"]):
                    exported_step = exported["workflow_steps"][index]
                    rules = step.pop("notification_rules")
                    self.assert_contains(exported_step, step, step["key"])
                    self.assertCountEqual(
                        [
                            self.rule_key(rule)
                            for rule in exported_step["notification_rules"]
                        ],
                        [self.rule_key(rule) for rule in rules],
                        step["key"],
                    )
                # The forms travel whole: what the template defines, the
                # imported call exports again.
                self.assertEqual(
                    [form["name"] for form in exported["checklists"]],
                    [form["name"] for form in template.get("checklists", [])],
                )
                for form, exported_form in zip(
                    template.get("checklists", []), exported["checklists"]
                ):
                    self.assert_form_contains(exported_form, form)

    def assert_contains(self, actual, expected, context):
        for key, value in expected.items():
            if key == "reference_code" and not value:
                continue
            self.assertIn(key, actual, f"{context}.{key}")
            if (
                isinstance(value, list)
                and value
                and all(isinstance(item, dict) for item in value)
            ):
                # A list of entries -- a step's criteria, say -- holds what
                # the template sets; the export adds the defaults it left out.
                self.assertEqual(len(actual[key]), len(value), f"{context}.{key}")
                for index, item in enumerate(value):
                    self.assert_contains(
                        actual[key][index], item, f"{context}.{key}[{index}]"
                    )
                continue
            self.assertEqual(actual[key], value, f"{context}.{key}")

    def assert_form_contains(self, actual, expected):
        name = expected["name"]
        self.assertEqual(len(actual["questions"]), len(expected["questions"]), name)
        for index, question in enumerate(expected["questions"]):
            context = f"{name}.questions[{index}]"
            exported = actual["questions"][index]
            for key, value in question.items():
                if key == "options":
                    self.assertEqual(
                        [o["label"] for o in exported["options"]],
                        [o["label"] for o in value],
                        context,
                    )
                elif key == "dependencies":
                    self.assertEqual(
                        [
                            (d["depends_on"], d["operator"], d["required_answer_value"])
                            for d in exported["dependencies"]
                        ],
                        [
                            (d["depends_on"], d["operator"], d["required_answer_value"])
                            for d in value
                        ],
                        context,
                    )
                elif key in ("min_value", "max_value"):
                    self.assertEqual(
                        decimal.Decimal(exported[key]), decimal.Decimal(value), context
                    )
                else:
                    self.assertEqual(exported[key], value, f"{context}.{key}")

    @staticmethod
    def rule_key(rule):
        return (
            rule["trigger"],
            rule["recipient"],
            rule["days_before"],
            rule["is_enabled"],
            rule["proposal_role"],
        )

    def test_extreme_scale_template(self):
        response = self.import_template("extreme_scale")
        call = models.Call.objects.get(uuid=response.data["call_uuid"])

        self.assertEqual(call.state, CallStates.DRAFT)
        self.assertEqual(call.manager, self.manager)
        self.assertEqual(call.evaluation_start, EvaluationStart.AT_CUTOFF)
        self.assertEqual(call.publish_results, ResultsPublication.WITH_ROUND)
        self.assertFalse(call.reviews_visible_to_submitters)
        self.assertFalse(call.reviewer_identity_visible_to_submitters)
        # Rounds and partitions belong to the portal importing the template.
        self.assertFalse(call.round_set.exists())
        self.assertFalse(call.requestedoffering_set.exists())

        role = call.proposal_roles.get()
        self.assertEqual(role.key, "rapporteur")
        self.assertEqual(role.eligible_from, ProposalRoleEligibility.CALL_PANEL_MEMBER)
        self.assertEqual((role.min_holders, role.max_holders), (2, 2))
        self.assertTrue(role.allows_lead)
        role_source = f"proposal_role:{role.pk}"

        steps = list(call.workflow_steps.order_by("order"))
        self.assertEqual(
            [(s.key, s.kind, s.outcome_set) for s in steps], EXTREME_SCALE_STEPS
        )
        # The extension (the tender's steps 14.a-c) and the final report (15)
        # run on the accepted proposal, after the evaluation.
        self.assertEqual(
            [s.phase for s in steps[-5:]],
            [WorkflowPhases.EXTENSION] * 3 + [WorkflowPhases.CLOSING] * 2,
        )
        self.assertEqual(models.CallWorkflowStep.phase_structure_errors(call.pk), [])
        self.assertEqual([s.order for s in steps], list(range(len(steps))))
        self.assertTrue(all(s.is_enabled for s in steps))
        step = {s.key: s for s in steps}

        # The applicant sees the eligibility check by name: it is the first
        # thing that happens to a submitted proposal, and an anonymous stand-in
        # there tells them nothing.
        self.assertTrue(step["administrative_check"].applicant_visible)

        technical = step["technical_assessment"]
        self.assertEqual(
            technical.contributor_source, ContributorSources.REQUESTED_OFFERING_TEAM
        )
        self.assertTrue(technical.per_requested_offering)
        self.assertEqual(technical.on_negative, OnNegativeOutcomes.CONTINUE)

        assignment = step["rapporteur_assignment"]
        self.assertEqual(list(assignment.assigns_roles.all()), [role])
        self.assertTrue(assignment.requires_coi_confirmation)
        self.assertFalse(step["reviewer_assignment"].assigns_roles.exists())

        questions = step["rapporteur_questions"]
        self.assertTrue(questions.collects_reviewer_suggestions)
        self.assertEqual(questions.contributor_source, role_source)
        self.assertEqual(questions.completer, ResponsibleRoles.PROPOSAL_ROLE)
        self.assertEqual(questions.completer_proposal_role, role)

        review = step["scientific_review"]
        self.assertEqual(
            review.contributor_source, ContributorSources.ASSIGNED_REVIEWERS
        )
        self.assertEqual((review.min_contributions, review.max_contributions), (3, 3))
        self.assertTrue(review.blind_review)
        self.assertEqual(models.CallWorkflowStep.review_step_for(call), review)
        self.assertEqual(
            list(review.criteria.values_list("name", flat=True)),
            [
                "Excellence",
                "Innovation and impact",
                "Quality and efficiency of the implementation",
            ],
        )

        response_step = step["response_to_reviews"]
        self.assertEqual(response_step.completer, ResponsibleRoles.APPLICANT)
        # The applicant answers the technical and the scientific referees.
        self.assertCountEqual(
            response_step.input_steps.all(), [technical, questions, review]
        )
        self.assertTrue(response_step.responds_to_inputs)
        # Whether or not the applicant answers, a decision follows.
        self.assertEqual(models.CallWorkflowStep.undecided_response_steps(call.pk), [])

        individual = step["individual_reports"]
        self.assertEqual(individual.contributor_source, role_source)
        self.assertEqual(individual.completer_proposal_role, role)
        self.assertEqual(
            (individual.min_contributions, individual.max_contributions), (2, 2)
        )
        self.assertCountEqual(
            individual.input_steps.all(),
            [technical, questions, review, response_step],
        )
        self.assertFalse(individual.release_to_applicant)
        # Both rapporteurs write an individual report...
        self.assertFalse(individual.contributors_lead_only)

        consolidated = step["consolidated_report"]
        self.assertEqual(consolidated.contributor_source, role_source)
        self.assertEqual(consolidated.completer_proposal_role, role)
        self.assertEqual(
            (consolidated.min_contributions, consolidated.max_contributions), (1, 1)
        )
        self.assertTrue(consolidated.release_to_applicant)
        # ...and the lead alone consolidates them.
        self.assertTrue(consolidated.contributors_lead_only)
        self.assertTrue(role.allows_lead)
        self.assertIn(individual, consolidated.input_steps.all())

        # The committee's chair and vice-chair report on the round's meeting;
        # the Lead Rapporteur then writes each proposal's final feedback.
        meeting = step["committee_meeting"]
        self.assertTrue(meeting.round_scoped)
        self.assertEqual(meeting.completer, ResponsibleRoles.PANEL_CHAIR)
        self.assertEqual(meeting.contributor_source, "call_role:panel_chair")
        feedback = step["final_feedback"]
        self.assertFalse(feedback.round_scoped)
        self.assertEqual(feedback.completer_proposal_role, role)
        self.assertTrue(feedback.contributors_lead_only)
        self.assertTrue(feedback.release_to_applicant)
        for key in ("committee_meeting", "final_feedback"):
            self.assertFalse(step[key].is_allocation_decision)

        allocation = step["allocation_decision"]
        self.assertEqual(
            models.CallWorkflowStep.allocation_decision_for(call), allocation
        )
        self.assertTrue(allocation.include_award_response)
        self.assertEqual(allocation.allocation_time, AllocationTimes.ON_DECISION)
        award = step["award_response"]
        self.assertEqual(award.completer, ResponsibleRoles.APPLICANT)
        self.assertEqual(award.on_negative, OnNegativeOutcomes.CANCEL)

        # Every step keeps the reminders a new step gets; the template adds
        # the ones that tell each step's people their work has started.
        for call_step in steps:
            self.assertTrue(
                call_step.notification_rules.filter(
                    trigger=NotificationRuleTriggers.DEADLINE_APPROACHING,
                    recipient=NotificationRuleRecipients.CALL_MANAGERS,
                    days_before=1,
                ).exists(),
                call_step.key,
            )
        self.assertTrue(
            assignment.notification_rules.filter(
                trigger=NotificationRuleTriggers.STEP_COMPLETED,
                recipient=NotificationRuleRecipients.PROPOSAL_ROLE,
                proposal_role=role,
            ).exists()
        )
        self.assertTrue(
            review.notification_rules.filter(
                trigger=NotificationRuleTriggers.STEP_STARTED,
                recipient=NotificationRuleRecipients.ASSIGNED_REVIEWERS,
            ).exists()
        )
        for call_step in (technical, questions, individual, consolidated):
            self.assertTrue(
                call_step.notification_rules.filter(
                    trigger=NotificationRuleTriggers.STEP_STARTED,
                    recipient=NotificationRuleRecipients.RESPONSIBLE_ROLE,
                ).exists(),
                call_step.key,
            )
        self.assertTrue(
            award.notification_rules.filter(
                trigger=NotificationRuleTriggers.STEP_STARTED,
                recipient=NotificationRuleRecipients.APPLICANT,
            ).exists()
        )

    def test_extreme_scale_steps_have_their_forms(self):
        response = self.import_template("extreme_scale")
        call = models.Call.objects.get(uuid=response.data["call_uuid"])
        step = {s.key: s for s in call.workflow_steps.select_related("checklist")}

        def questions(key):
            checklist = step[key].checklist
            self.assertIsNotNone(checklist, key)
            self.assertEqual(
                checklist.checklist_type, checklist_enums.ChecklistTypes.WORKFLOW_STEP
            )
            return [
                (q.description, q.question_type, q.required)
                for q in checklist.questions.order_by("order")
            ]

        self.assertEqual(
            questions("response_to_reviews"),
            [
                (
                    "Response to the reviews and questions",
                    QuestionTypes.RICH_TEXT,
                    True,
                ),
                ("Supporting document", QuestionTypes.FILE, False),
            ],
        )
        self.assertEqual(
            questions("rapporteur_questions"),
            [("Questions for the applicant", QuestionTypes.RICH_TEXT, False)],
        )
        # The rapporteurs score the criteria the scientific reviewers assess.
        criteria = list(
            step["scientific_review"]
            .criteria.order_by("order")
            .values_list("name", flat=True)
        )
        self.assertEqual(len(criteria), 3)
        self.assertEqual(
            questions("individual_reports"),
            [("Assessment of the proposal", QuestionTypes.RICH_TEXT, True)]
            + [(f"Score: {name}", QuestionTypes.RATING, True) for name in criteria],
        )
        self.assertEqual(
            questions("consolidated_report"),
            [
                (
                    "Consolidated feedback for the applicant",
                    QuestionTypes.RICH_TEXT,
                    True,
                ),
                ("Recommendation", QuestionTypes.SINGLE_SELECT, True),
                ("Conditions", QuestionTypes.TEXT_AREA, False),
            ]
            # The Lead Rapporteur grades the proposal on the same criteria.
            + [(f"Score: {name}", QuestionTypes.RATING, True) for name in criteria],
        )
        consolidated = step["consolidated_report"].checklist
        recommendation, conditions = consolidated.questions.order_by("order")[1:3]
        self.assertEqual(
            list(recommendation.question_options.values_list("label", flat=True)),
            ["Fund", "Fund with conditions", "Do not fund"],
        )
        dependency = conditions.dependencies.get()
        self.assertEqual(dependency.depends_on_question, recommendation)
        self.assertEqual(
            dependency.required_answer_value,
            [
                recommendation.question_options.get(
                    label="Fund with conditions"
                ).uuid.hex
            ],
        )
        # The scientific reviewers write reviews; the technical assessment
        # takes each partition's outcome and comment.
        for key in ("scientific_review", "technical_assessment"):
            self.assertIsNone(step[key].checklist, key)

    def test_each_import_makes_its_own_forms(self):
        first = models.Call.objects.get(
            uuid=self.import_template("extreme_scale").data["call_uuid"]
        )
        second = models.Call.objects.get(
            uuid=self.import_template("extreme_scale").data["call_uuid"]
        )
        first_form = first.workflow_steps.get(key="response_to_reviews").checklist
        second_form = second.workflow_steps.get(key="response_to_reviews").checklist
        self.assertNotEqual(first_form, second_form)
        self.assertEqual(first_form.name, "Response to reviews")
        self.assertEqual(second_form.name, f"Response to reviews ({second.name})")

    def test_regular_access_routes_by_domain_panel(self):
        template = call_templates.load("regular_access")
        call = models.Call.objects.get(
            uuid=self.import_template("regular_access").data["call_uuid"]
        )
        panels = list(call.panels.order_by("order"))
        self.assertEqual(len(panels), 5)
        self.assertEqual(
            [sorted(p.science_domains.values_list("code", flat=True)) for p in panels],
            [
                sorted(d["code"] for d in p["science_domains"])
                for p in template["panels"]
            ],
        )
        roles = {role.key: role for role in call.proposal_roles.all()}
        self.assertEqual(
            roles["domain_panel_chair"].eligible_from,
            ProposalRoleEligibility.PANEL_CHAIR,
        )
        self.assertEqual(
            roles["rapporteur"].eligible_from, ProposalRoleEligibility.PANEL_MEMBER
        )
        exported, _ = call_transfer.export_call(call)
        self.assertEqual(
            [
                (p["key"], p["science_domains"], p["science_sub_domains"])
                for p in exported["panels"]
            ],
            [
                (p["key"], p["science_domains"], p["science_sub_domains"])
                for p in template["panels"]
            ],
        )

    def test_meetings_run_once_per_round(self):
        expected = {
            "extreme_scale": {"committee_meeting"},
            "regular_access": {"domain_panel_meeting", "super_panel_meeting"},
            "ai_factory_large_scale": {"ranking_list_adoption"},
            "single_panel_review": {"panel_meeting"},
            "technical_and_scientific_review": {"panel_meeting"},
            "access_maximize": {"panel_meeting"},
            "alcc": {"panel_meeting"},
            "domain_sub_panels": {"sub_panel_meeting", "panel_meeting"},
            "beamtime_standard": {"review_panel_meeting"},
        }
        for name in call_templates.names():
            with self.subTest(template=name):
                call = models.Call.objects.get(
                    uuid=self.import_template(name).data["call_uuid"]
                )
                round_steps = call.workflow_steps.filter(round_scoped=True)
                self.assertEqual(
                    set(round_steps.values_list("key", flat=True)),
                    expected.get(name, set()),
                )
                for step in round_steps:
                    self.assertIsNone(step.round_scoped_refusal(), step.key)
        regular = models.Call.objects.filter(name__icontains="Regular").last()
        domain = regular.workflow_steps.get(key="domain_panel_meeting")
        self.assertTrue(
            domain.contributor_source.startswith(ContributorSources.PROPOSAL_ROLE)
        )
        self.assertEqual(
            regular.workflow_steps.get(key="super_panel_meeting").contributor_source,
            f"{ContributorSources.CALL_ROLE}:{ResponsibleRoles.PANEL_CHAIR}",
        )

    def test_technical_assessments_wait_for_the_call_managers(self):
        """So a submitted assessment can still be sent back before the
        proposal moves on; the round's proposals are moved on together."""
        for name in (
            "extreme_scale",
            "regular_access",
            "ai_for_science",
            "ai_factory_large_scale",
            "benchmark_access",
            "development_access",
            "quantum_access",
        ):
            with self.subTest(template=name):
                call = models.Call.objects.get(
                    uuid=self.import_template(name).data["call_uuid"]
                )
                self.assertEqual(
                    call.workflow_steps.get(key="technical_assessment").transition_mode,
                    "manual",
                )

    def test_template_name_can_be_overridden(self):
        user = structure_factories.UserFactory()
        call, sections, warnings = call_templates.import_template(
            "extreme_scale", self.manager, user, call_name="Extreme Scale 2027"
        )
        self.assertEqual(call.name, "Extreme Scale 2027")
        self.assertEqual(call.created_by, user)
        self.assertEqual(warnings, [])

    def test_unknown_template_is_refused(self):
        with self.assertRaises(KeyError):
            call_templates.load("../extreme_scale")
        # The programme registry is no template.
        with self.assertRaises(KeyError):
            call_templates.load("programmes")


# The presets modelled on processes beyond EuroHPC.
CATALOGUE_PRESETS = {
    "rapid_access",
    "staff_reviewed_allocation",
    "single_panel_review",
    "technical_and_scientific_review",
    "access_maximize",
    "alcc",
    "nairr_research",
    "nairr_classroom",
    "domain_sub_panels",
    "beamtime_standard",
    "beamtime_rapid",
}

DOMAIN_NOT_FOUND = re.compile(
    r"^Science (sub-)?domain '[0-9.]+' was not found on this portal, skipped\.$"
)


class CatalogueTemplatesTest(test.APITestCase):
    """Every shipped preset imports, checks and describes cleanly."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.manager = factories.CallManagingOrganisationFactory()

    def test_the_catalogue_ships(self):
        self.assertTrue(CATALOGUE_PRESETS <= set(call_templates.names()))

    def test_on_a_portal_without_science_domains_only_domains_are_missed(self):
        # No science domains are loaded here: a panel's domains are the only
        # thing an import may not find.
        for name in call_templates.names():
            with self.subTest(template=name):
                _, _, warnings = call_templates.import_template(
                    name, self.manager, self.staff
                )
                self.assertEqual(
                    [w for w in warnings if not DOMAIN_NOT_FOUND.match(w)], []
                )

    def test_the_checker_finds_no_errors_in_any_preset(self):
        for name in call_templates.names():
            with self.subTest(template=name):
                findings = workflow_checks.check(call_templates.load(name))
                self.assertEqual(workflow_checks.errors(findings), [])

    def test_every_preset_is_described(self):
        for name in call_templates.names():
            with self.subTest(template=name):
                description = workflow_description.describe(call_templates.load(name))
                self.assertEqual(
                    [step["key"] for step in description["steps"]],
                    [
                        step["key"]
                        for step in call_templates.load(name)["workflow_steps"]
                    ],
                )

    def test_panel_presets_route_by_science_domain(self):
        load_preset("cscs")
        expected = {
            "domain_sub_panels": {
                "1.1": "astronomy-cosmology",
                "1.2": "particle-nuclear",
            },
            "beamtime_standard": {
                "4.1": "life-sciences",
                "2.3": "chemistry-materials",
                "3.2": "earth-environment",
                "1.1": "physics-engineering",
                "5.1": "physics-engineering",
            },
        }
        for name, routes in expected.items():
            call, _, warnings = call_templates.import_template(
                name, self.manager, self.staff
            )
            self.assertEqual(warnings, [])
            for code, panel in routes.items():
                with self.subTest(template=name, sub_domain=code):
                    sub_domain = ScienceSubDomain.objects.get(code=code)
                    self.assertEqual(panels.panel_for(call, sub_domain).key, panel)

    def test_the_generic_presets_are_continuous_or_at_cutoff(self):
        settings = {
            name: call_templates.load(name)["call"] for name in CATALOGUE_PRESETS
        }
        rolling = {
            "rapid_access",
            "staff_reviewed_allocation",
            "nairr_classroom",
            "beamtime_rapid",
        }
        for name, call in settings.items():
            with self.subTest(template=name):
                self.assertEqual(
                    call["evaluation_start"],
                    EvaluationStart.ON_SUBMISSION
                    if name in rolling
                    else EvaluationStart.AT_CUTOFF,
                )
        # The rapid presets spend the round's budgets first come, first served.
        for name in ("rapid_access", "staff_reviewed_allocation"):
            self.assertEqual(settings[name]["budget_enforcement"], "hard")
        steps = {
            step["key"]: step
            for step in call_templates.load("rapid_access")["workflow_steps"]
        }
        self.assertTrue(steps["allocation_decision"]["decides_automatically"])
