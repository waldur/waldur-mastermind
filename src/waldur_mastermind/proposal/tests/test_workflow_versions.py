"""Workflow versions: where a call came from, its history and what changed."""

import copy
import datetime
from unittest import mock

from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.server import openapi_settings
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_templates,
    models,
    views,
    workflow_diff,
    workflow_versions,
)
from waldur_mastermind.proposal.enums import CallStates
from waldur_mastermind.proposal.tests import factories, fixtures

Reasons = models.WorkflowVersion.Reasons


def step(key, order, **values):
    return {
        "key": key,
        "name": key.capitalize(),
        "kind": "decision",
        "outcome_set": "eligibility",
        "completer": "call_manager",
        "order": order,
        **values,
    }


def document(*steps, **call):
    return {
        "schema_version": 3,
        "call": {"name": "Call", **call},
        "proposal_roles": [],
        "workflow_steps": list(steps),
    }


def changed(changes, key):
    return next(s for s in changes["steps"] if s["key"] == key)


class DiffTest(SimpleTestCase):
    def test_a_rename_is_a_cosmetic_change(self):
        old = document(step("check", 0))
        new = document(step("check", 0, name="Eligibility check"))
        entry = changed(workflow_diff.diff(old, new), "check")
        self.assertEqual(entry["change"], "changed")
        self.assertEqual(entry["kind"], "cosmetic")
        self.assertEqual(entry["old_name"], "Check")
        self.assertEqual(entry["fields"][0]["field"], "name")

    def test_a_field_change_is_behaviour(self):
        old = document(step("check", 0, duration_in_days=7))
        new = document(step("check", 0, duration_in_days=14))
        entry = changed(workflow_diff.diff(old, new), "check")
        self.assertEqual(entry["kind"], "behaviour")
        self.assertEqual(
            (entry["fields"][0]["old"], entry["fields"][0]["new"]), (7, 14)
        )

    def test_values_written_two_ways_are_the_same(self):
        old = document(step("check", 0, min_score_threshold="3.0"))
        new = document(step("check", 0, min_score_threshold="3.00"))
        self.assertEqual(workflow_diff.diff(old, new)["steps"], [])
        # A template leaves defaults out; an export carries them.
        self.assertEqual(
            workflow_diff.diff(
                document(step("check", 0)),
                document(step("check", 0, is_enabled=True, blind_review=False)),
            )["steps"],
            [],
        )

    def test_sets_report_members_added_and_removed(self):
        old = document(step("a", 0), step("b", 1), step("c", 2, input_steps=["a"]))
        new = document(step("a", 0), step("b", 1), step("c", 2, input_steps=["b"]))
        field = changed(workflow_diff.diff(old, new), "c")["fields"][0]
        self.assertEqual(field["field"], "input_steps")
        self.assertEqual((field["added"], field["removed"]), (["b"], ["a"]))

    def test_notification_rules_are_keyed(self):
        rule = {"trigger": "step_started", "recipient": "applicant"}
        old = document(step("a", 0, notification_rules=[rule]))
        new = document(
            step(
                "a",
                0,
                notification_rules=[
                    {**rule, "is_enabled": False},
                    {"trigger": "step_completed", "recipient": "applicant"},
                ],
            )
        )
        fields = changed(workflow_diff.diff(old, new), "a")["fields"]
        self.assertIn("step_completed/applicant//", fields[0]["added"])
        self.assertEqual(
            fields[1]["field"],
            "notification_rules[step_started/applicant//].is_enabled",
        )

    def test_criteria_weigh_1_unless_they_say_otherwise(self):
        # A template leaves the weight out; an export carries it.
        old = document(step("a", 0, criteria=[{"name": "Merit", "order": 0}]))
        same = document(
            step("a", 0, criteria=[{"name": "Merit", "order": 0, "weight": 1}])
        )
        self.assertEqual(workflow_diff.diff(old, same)["steps"], [])
        heavier = document(
            step("a", 0, criteria=[{"name": "Merit", "order": 0, "weight": 3}])
        )
        field = changed(workflow_diff.diff(old, heavier), "a")["fields"][0]
        self.assertEqual(field["field"], "criteria[Merit].weight")
        self.assertEqual(
            (field["old"], field["new"], field["kind"]), (1, 3, "behaviour")
        )

    def test_steps_added_and_removed(self):
        old = document(step("a", 0), step("b", 1))
        new = document(step("a", 0), step("c", 1))
        changes = workflow_diff.diff(old, new)
        self.assertEqual(changed(changes, "c")["change"], "added")
        self.assertEqual(changed(changes, "b")["change"], "removed")
        self.assertEqual(changes["summary"]["steps_added"], 1)

    def test_inserting_a_step_moves_no_other(self):
        old = document(step("a", 0), step("b", 1), step("c", 2))
        new = document(step("a", 0), step("x", 1), step("b", 2), step("c", 3))
        changes = workflow_diff.diff(old, new)
        self.assertEqual([s["key"] for s in changes["steps"]], ["x"])

    def test_a_swap_moves_one_step(self):
        old = document(step("a", 0), step("b", 1), step("c", 2))
        new = document(step("b", 0), step("a", 1), step("c", 2))
        changes = workflow_diff.diff(old, new)
        moved = [s["key"] for s in changes["steps"] if s["moved"]]
        self.assertEqual(len(moved), 1)
        self.assertEqual(changes["steps"][0]["change"], "moved")
        self.assertEqual(changes["steps"][0]["kind"], "behaviour")

    def test_call_settings_are_diffed_flat(self):
        changes = workflow_diff.diff(
            document(step("a", 0), target_decision_days=30, description="A"),
            document(step("a", 0), target_decision_days=45, description="B"),
        )
        kinds = {c["field"]: c["kind"] for c in changes["call"]}
        self.assertEqual(
            kinds, {"target_decision_days": "behaviour", "description": "cosmetic"}
        )


class TemplateVersionTest(test.APITestCase):
    def setUp(self):
        # The system roles a call is seeded with exist before a template's
        # trial import creates (and rolls back) a call of its own.
        self.fixture = fixtures.ProposalFixture()
        self.organizer = self.fixture.call_organizer_user
        self.fixture.new_call
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.template = models.CallTemplate.objects.create(
            name="Template",
            document=call_templates.load("extreme_scale"),
            schema_version=3,
        )

    def test_editing_the_document_increments_the_version(self):
        document = copy.deepcopy(self.template.document)
        document["workflow_steps"][0]["duration_in_days"] = 99
        self.template.document = document
        self.template.save()
        self.assertEqual(self.template.version, 2)

    def test_name_description_and_publication_do_not(self):
        self.template.name = "Renamed"
        self.template.is_published = True
        self.template.description = "Other"
        self.template.save()
        self.template.refresh_from_db()
        self.assertEqual(self.template.version, 1)

    def test_api_edits_record_versions_for_staff_only(self):
        url = "http://testserver" + reverse(
            "call-template-detail", kwargs={"uuid": self.template.uuid.hex}
        )
        document = copy.deepcopy(self.template.document)
        document["workflow_steps"][0]["duration_in_days"] = 99
        self.client.force_authenticate(self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(url, {"document": document}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        versions = list(workflow_versions.owner_versions(self.template))
        self.assertEqual([v.number for v in versions], [2])
        versions_url = url + "workflow-versions/"
        self.assertEqual(self.client.get(versions_url).status_code, status.HTTP_200_OK)
        diff = self.client.get(url + "workflow-diff/?from=2&to=current")
        self.assertEqual(diff.status_code, status.HTTP_200_OK, diff.data)
        self.template.is_published = True
        self.template.save()
        self.client.force_authenticate(self.organizer)
        self.assertEqual(
            self.client.get(versions_url).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_loading_a_preset_records_the_first_version(self):
        call_templates.load_presets(["extreme_scale"])
        template = models.CallTemplate.objects.get(name="Extreme Scale Access")
        version = workflow_versions.owner_versions(template).get()
        self.assertEqual(version.reason, Reasons.CREATED)
        self.assertEqual(version.number, 1)


class CreatedFromTemplateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.user = self.fixture.call_organizer_user

    def create(self, name):
        template = models.CallTemplate.objects.create(
            name=name,
            document=call_templates.load(name),
            schema_version=3,
            is_published=True,
        )
        workflow_versions.record(template)
        self.client.force_authenticate(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                factories.CallFactory.get_protected_list_url("import_call"),
                {
                    "manager": self.fixture.manager.uuid.hex,
                    "template": template.uuid.hex,
                },
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return template, models.Call.objects.get(uuid=response.data["call_uuid"])

    def test_the_call_remembers_its_template_and_baseline(self):
        template, call = self.create("extreme_scale")
        self.assertEqual((call.template, call.template_version), (template, 1))
        versions = list(workflow_versions.owner_versions(call))
        self.assertEqual(versions[0].reason, Reasons.CREATED_FROM_TEMPLATE)
        self.assertEqual(versions[0].document, template.document)
        self.assertEqual(versions[1].reason, Reasons.IMPORTED)

    def test_an_unchanged_call_has_no_behaviour_changes_from_any_template(self):
        for name in call_templates.names():
            with self.subTest(template=name):
                _, call = self.create(name)
                changes = workflow_diff.diff(
                    workflow_versions.resolve(call, "template"),
                    workflow_versions.call_document(call),
                )
                behaviour = [
                    (s["key"], [f["field"] for f in s["fields"]])
                    for s in changes["steps"]
                    + changes["proposal_roles"]
                    + changes["forms"]
                    if s["kind"] == "behaviour"
                ] + [c["field"] for c in changes["call"] if c["kind"] == "behaviour"]
                self.assertEqual(behaviour, [])

    def test_changes_from_template_endpoint(self):
        template, call = self.create("extreme_scale")
        step = call.workflow_steps.get(key="administrative_check")
        url = factories.CallWorkflowStepFactory.get_url(call, step)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(url, {"duration_in_days": 30})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self.client.get(
            factories.CallFactory.get_protected_url(call, "workflow-diff")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["template"]["name"], template.name)
        entry = changed(response.data["changes"], "administrative_check")
        self.assertEqual(entry["fields"][0]["field"], "duration_in_days")
        check = self.client.get(
            factories.CallFactory.get_protected_url(call, "workflow-check")
        )
        deviations = [
            f for f in check.data["findings"] if f["code"] == "deviates_from_template"
        ]
        self.assertEqual([f["step"] for f in deviations], ["administrative_check"])

    def test_the_latest_template_only_while_it_is_published(self):
        template, call = self.create("extreme_scale")
        url = (
            factories.CallFactory.get_protected_url(call, "workflow-diff")
            + "?from=template&to=template_latest"
        )
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)
        template.is_published = False
        template.save()
        self.assertEqual(self.client.get(url).status_code, status.HTTP_400_BAD_REQUEST)


class RecordingTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.new_call
        self.user = self.fixture.staff
        self.step = self.call.workflow_steps.filter(is_enabled=True).first()

    def patch(self, values, user=None):
        self.client.force_authenticate(user or self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                factories.CallWorkflowStepFactory.get_url(self.call, self.step), values
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def numbers(self):
        return list(
            workflow_versions.owner_versions(self.call).values_list("number", "reason")
        )

    def test_a_step_edit_records_a_version_only_when_the_document_changes(self):
        workflow_versions.ensure_baseline(self.call)
        self.patch({"duration_in_days": 30})
        self.assertEqual(self.numbers(), [(1, Reasons.IMPORTED), (2, Reasons.EDITED)])
        self.patch({"duration_in_days": 30})
        self.assertEqual(len(self.numbers()), 2)

    def test_draft_edits_by_one_user_coalesce(self):
        workflow_versions.ensure_baseline(self.call)
        self.patch({"duration_in_days": 30})
        self.patch({"duration_in_days": 31})
        versions = list(workflow_versions.owner_versions(self.call))
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[1].summary["steps_changed"], 1)
        with freeze_time(timezone.now() + datetime.timedelta(minutes=20)):
            self.patch({"duration_in_days": 32})
        self.assertEqual(len(self.numbers()), 3)
        other = structure_factories.UserFactory(is_staff=True)
        self.patch({"duration_in_days": 33}, user=other)
        self.assertEqual(len(self.numbers()), 4)

    def test_after_activation_every_edit_is_kept(self):
        workflow_versions.ensure_baseline(self.call)
        self.call.state = CallStates.ACTIVE
        self.call.save()
        self.patch({"duration_in_days": 30})
        self.patch({"duration_in_days": 31})
        self.assertEqual(len(self.numbers()), 3)

    def test_every_write_action_is_classified(self):
        viewset = views.ProtectedCallViewSet
        actions = {"create", "update", "partial_update", "destroy"}
        for extra in viewset.get_extra_actions():
            if set(extra.mapping) - {"get", "head", "options"}:
                actions.add(extra.__name__)
        for name in dir(viewset):
            if not name.startswith("_") and (
                name.endswith("_detail") or name.endswith("_round")
            ):
                actions.add(name)
        classified = (
            viewset.VERSIONED_ACTIONS
            | viewset.VERSION_SELF_RECORDING_ACTIONS
            | viewset.VERSION_EXEMPT_ACTIONS
        )
        self.assertEqual(actions - classified, set())
        self.assertEqual(
            viewset.VERSIONED_ACTIONS & viewset.VERSION_EXEMPT_ACTIONS, set()
        )

    def test_a_panel_written_through_the_api_records_a_version(self):
        workflow_versions.ensure_baseline(self.call)
        self.client.force_authenticate(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                factories.CallFactory.get_protected_url(self.call, "panels"),
                {"name": "Earth System Sciences"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        version = workflow_versions.owner_versions(self.call).last()
        self.assertEqual(version.number, 2)
        self.assertEqual(
            [panel["name"] for panel in version.document["panels"]],
            ["Earth System Sciences"],
        )

    def test_a_versioned_write_path_records(self):
        self.client.force_authenticate(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                factories.CallFactory.get_protected_url(self.call),
                {"target_decision_days": 40},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        version = workflow_versions.owner_versions(self.call).last()
        self.assertEqual(version.document["call"]["target_decision_days"], 40)

    def test_a_failed_write_records_nothing(self):
        with mock.patch.object(workflow_versions, "record") as record:
            self.client.force_authenticate(self.user)
            with self.captureOnCommitCallbacks(execute=True):
                self.client.patch(
                    factories.CallWorkflowStepFactory.get_url(self.call, self.step),
                    {"outcome_set": "nonsense"},
                )
            record.assert_not_called()


class VersionEndpointsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call

    def test_call_team_staff_and_support_read_the_history(self):
        url = factories.CallFactory.get_protected_url(self.call, "workflow-versions")
        for user in (
            self.fixture.call_manager,
            self.fixture.staff,
            structure_factories.UserFactory(is_support=True),
        ):
            with self.subTest(user=user):
                self.client.force_authenticate(user)
                response = self.client.get(url)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data[0]["number"], 1)
        detail = url + "1/"
        self.assertEqual(self.client.get(detail).status_code, status.HTTP_200_OK)
        for user in (
            self.fixture.panel_member,
            self.fixture.reviewer_1,
            self.fixture.proposal_creator,
            structure_factories.UserFactory(),
        ):
            with self.subTest(user=user):
                self.client.force_authenticate(user)
                for target in (
                    url,
                    detail,
                    factories.CallFactory.get_protected_url(self.call, "workflow-diff"),
                ):
                    self.assertEqual(
                        self.client.get(target).status_code, status.HTTP_404_NOT_FOUND
                    )

    def test_unknown_versions_are_refused(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CallFactory.get_protected_url(self.call, "workflow-diff")
        self.assertEqual(
            self.client.get(url + "?from=7").status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertEqual(
            self.client.get(url + "?from=1&to=current").status_code,
            status.HTTP_200_OK,
        )


class SchemaNamesTest(SimpleTestCase):
    def test_the_reason_enum_name_follows_the_model(self):
        self.assertEqual(
            tuple(
                openapi_settings.SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"][
                    "WorkflowVersionReason"
                ]
            ),
            tuple(Reasons.CHOICES),
        )
