"""Saved scenarios of call templates.

Staff write them on a template; they run whenever the template changes, and
the results are kept on its latest version. A call created from a template
can run its scenarios. Scenarios are dry runs: nothing they do survives, and
no mail leaves.
"""

import copy
from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.fixtures import OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import (
    call_templates,
    models,
    serializers,
    tasks,
    workflow_observability,
    workflow_scenarios,
    workflow_versions,
)
from waldur_mastermind.proposal.enums import RequestedOfferingStates
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_workflow_dry_run import (
    THROTTLE,
    DryRunMixin,
    row_counts,
)

TEMPLATE = "benchmark_access"

SCENARIO = {
    "key": "accepted",
    "name": "Accepted",
    "description": "",
    "request": {
        "offerings": 1,
        "actions": [
            {"step": "technical_assessment", "outcome": "feasible"},
            {"advance": True},
        ],
    },
    "assertions": [
        {"proposal_state": "accepted"},
        {"step_status": {"step": "allocation_decision", "status": "completed"}},
        {"notified": {"actor": "applicant", "trigger": "proposal_decided"}},
    ],
}


def template_url(template, action=None):
    url = "http://testserver" + reverse(
        "call-template-detail", kwargs={"uuid": template.uuid.hex}
    )
    return url if action is None else url + action + "/"


def make_template(scenarios=(), name="Benchmark"):
    document = call_templates.load(TEMPLATE)
    template = models.CallTemplate.objects.create(
        name=name,
        document=document,
        schema_version=document["schema_version"],
        is_published=True,
        scenarios=list(scenarios),
    )
    workflow_versions.record(template, reason=workflow_versions.Reasons.CREATED)
    return template


class ScenarioTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.staff = self.fixture.staff
        throttle = mock.patch(THROTTLE, return_value=True)
        throttle.start()
        self.addCleanup(throttle.stop)


class ValidationTest(ScenarioTestBase):
    def patch(self, template, **payload):
        self.client.force_authenticate(self.staff)
        return self.client.patch(template_url(template), payload, format="json")

    def test_valid_lists_are_stored(self):
        template = make_template()
        response = self.patch(template, scenarios=[SCENARIO])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        template.refresh_from_db()
        self.assertEqual(template.scenarios[0]["key"], "accepted")
        self.assertEqual(
            template.scenarios[0]["request"]["actions"][1], {"advance": True}
        )

    def test_invalid_scenario_is_refused_with_a_field_error(self):
        template = make_template()
        for broken in (
            {**SCENARIO, "assertions": [{"proposal_state": "dreaming"}]},
            {**SCENARIO, "assertions": [{"ends_at": "x", "proposal_state": "draft"}]},
            {**SCENARIO, "request": {"actions": [{"step": "x"}]}},
            {**SCENARIO, "key": "not a slug"},
            [SCENARIO, SCENARIO],
        ):
            with self.subTest(scenario=broken):
                value = broken if isinstance(broken, list) else [broken]
                response = self.patch(template, scenarios=value)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("scenarios", response.data)

    def test_call_managers_never_read_nor_write_them(self):
        template = make_template(scenarios=[SCENARIO])
        self.client.force_authenticate(self.fixture.call_organizer_user)
        response = self.client.get(template_url(template))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for name in ("scenarios", "checks_summary", "document"):
            self.assertNotIn(name, response.data)
        response = self.client.patch(
            template_url(template), {"scenarios": []}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class LabelTextTest(test.APITestCase):
    def test_labels_compare_regardless_of_case_and_spacing(self):
        self.assertTrue(
            workflow_scenarios.same_text(
                "Administratively  rejected", "administratively rejected"
            )
        )
        self.assertFalse(workflow_scenarios.same_text("Rejected", "Accepted"))
        self.assertFalse(workflow_scenarios.same_text(None, "Rejected"))


class TemplateScenariosTest(ScenarioTestBase):
    """On a template, scenarios are definitions: nothing runs them there."""

    def test_saving_a_template_runs_nothing(self):
        self.client.force_authenticate(self.staff)
        template = make_template()
        with (
            mock.patch.object(tasks.run_call_workflow_scenarios, "delay") as delay,
            mock.patch.object(workflow_scenarios, "run_scenario") as run,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.patch(
                template_url(template), {"scenarios": [SCENARIO]}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        delay.assert_not_called()
        run.assert_not_called()
        self.assertFalse(
            any(v.checks for v in workflow_versions.owner_versions(template))
        )

    def test_a_template_has_no_run_of_its_own(self):
        template = make_template(scenarios=[SCENARIO])
        self.client.force_authenticate(self.staff)
        self.assertEqual(
            self.client.get(template_url(template, "checks")).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.post(template_url(template, "run-checks")).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertNotIn("checks_summary", self.client.get(template_url(template)).data)


class ShippedTemplatesTest(DryRunMixin, test.APITestCase):
    """Every shipped template's scenarios pass on a call created from it."""

    @pytest.mark.nightly
    def test_every_template_passes_its_scenarios(self):
        for name in call_templates.names():
            with self.subTest(template=name):
                scenarios = call_templates.load_scenarios(name)
                self.assertGreaterEqual(len(scenarios), 3)
                offerings = max(
                    int((s.get("request") or {}).get("offerings") or 1)
                    for s in scenarios
                )
                call = self.scenario_call(name, offerings=offerings)
                results = [
                    workflow_scenarios.run_scenario(call, scenario)
                    for scenario in scenarios
                ]
                failing = [
                    (r["key"], r["error"] or r["failure"])
                    for r in results
                    if r["status"] != workflow_scenarios.PASSED
                ]
                self.assertEqual(failing, [])

    def test_the_shipped_scenarios_are_valid(self):
        for name in call_templates.names():
            with self.subTest(template=name):
                serializers.validate_template_scenarios(
                    call_templates.load_scenarios(name)
                )

    def test_their_texts_name_steps_not_keys(self):
        for name in call_templates.names():
            for scenario in call_templates.load_scenarios(name):
                for field in ("name", "description"):
                    with self.subTest(template=name, scenario=scenario["key"]):
                        self.assertNotRegex(
                            scenario.get(field, ""), r"\b[a-z]+(_[a-z0-9]+)+\b"
                        )

    def test_a_loaded_preset_takes_them_with_the_document(self):
        call_templates.load_presets(["extreme_scale"])
        template = models.CallTemplate.objects.get(name="Extreme Scale Access")
        self.assertEqual(
            template.scenarios, call_templates.load_scenarios("extreme_scale")
        )


class CallChecksTest(ScenarioTestBase):
    def setUp(self):
        super().setUp()
        self.template = make_template(scenarios=[SCENARIO])
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_list_url("import_call"),
            {
                "manager": self.fixture.manager.uuid.hex,
                "template": self.template.uuid.hex,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.call = models.Call.objects.get(uuid=response.data["call_uuid"])
        self.call.add_user(self.fixture.call_manager, "CALL.MANAGER")
        offering = marketplace_factories.OfferingFactory()
        offering.add_user(structure_factories.UserFactory(), OfferingRole.MANAGER)
        factories.RequestedOfferingFactory(
            call=self.call,
            offering=offering,
            state=RequestedOfferingStates.ACCEPTED,
            created_by=self.fixture.owner,
        )

    def url(self, action):
        return factories.CallFactory.get_protected_url(self.call, action)

    def ask_for_a_run(self):
        with (
            mock.patch.object(tasks.run_call_workflow_scenarios, "delay") as delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(self.url("run-workflow-scenarios"))
        return response, delay

    def test_the_call_team_runs_the_scenarios_and_reads_their_results(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response, delay = self.ask_for_a_run()
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertEqual(response.data["status"], "running")
        self.assertEqual(response.data["results"], [])
        delay.assert_called_once_with(self.call.uuid.hex)

        before = row_counts()
        tasks.run_call_workflow_scenarios(self.call.uuid.hex)
        self.assertEqual(mail.outbox, [])
        counts = row_counts()
        self.assertEqual(counts, before)

        response = self.client.get(self.url("workflow-scenarios"))
        self.assertEqual(response.data["status"], "idle")
        self.assertIsNone(response.data["error"])
        self.assertEqual(response.data["totals"]["passed"], 1, response.data)
        self.assertEqual(response.data["results"][0]["status"], "passed")
        self.assertEqual(response.data["scenarios"][0]["key"], "accepted")

        # The paths proposals take are matched against what the scenarios walked.
        paths = workflow_observability.scenario_paths(self.call)
        self.assertEqual(paths[0][0], ("technical_assessment", "feasible"))

    def test_a_failing_assertion_names_the_first_that_failed(self):
        failing = copy.deepcopy(SCENARIO)
        failing["assertions"].insert(0, {"proposal_state": "rejected"})
        result = workflow_scenarios.run_scenario(self.call, failing)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["assertion"], {"proposal_state": "rejected"})

    def test_an_impossible_action_is_reported_as_an_error(self):
        broken = copy.deepcopy(SCENARIO)
        broken["request"]["actions"] = [{"step": "nowhere", "outcome": "feasible"}]
        result = workflow_scenarios.run_scenario(self.call, broken)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["action"], 0)

    def test_too_few_accepted_offerings_is_an_error_not_a_failure(self):
        scenario = copy.deepcopy(SCENARIO)
        scenario["request"]["offerings"] = 3
        self.template.scenarios = [scenario]
        self.template.save()
        result = workflow_scenarios.run_call_scenarios(self.call)
        self.assertEqual(result["scenarios"][0]["status"], "error")
        self.assertIn("3 offering", result["scenarios"][0]["error"])

    def test_who_reads_and_runs_them(self):
        self.client.force_authenticate(self.fixture.reviewer_1)
        self.assertEqual(
            self.client.get(self.url("workflow-scenarios")).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertIn(
            self.client.post(self.url("run-workflow-scenarios")).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        support = structure_factories.UserFactory(is_support=True)
        self.client.force_authenticate(support)
        self.assertEqual(
            self.client.get(self.url("workflow-scenarios")).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.post(self.url("run-workflow-scenarios")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_call_without_template_has_no_scenarios(self):
        call = self.fixture.call
        call.add_user(self.fixture.call_manager, "CALL.MANAGER")
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(
            factories.CallFactory.get_protected_url(call, "workflow-scenarios")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["scenarios"], [])
        self.assertIsNone(response.data["template"])

    def test_a_dry_run_offers_itself_as_a_scenario(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.post(
            self.url("workflow-dry-run"),
            {"actions": SCENARIO["request"]["actions"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        draft = response.data["scenario_draft"]
        self.assertEqual(draft["request"]["offerings"], 1)
        self.assertEqual(draft["request"]["actions"], SCENARIO["request"]["actions"])
        self.assertIn({"proposal_state": "accepted"}, draft["assertions"])
        # A draft, once named, is a valid scenario that passes on the call.
        draft = {**draft, "key": "saved", "name": "Saved"}
        serializers.validate_template_scenarios([draft])
        result = workflow_scenarios.run_scenario(self.call, draft)
        self.assertEqual(result["status"], "passed", result)
