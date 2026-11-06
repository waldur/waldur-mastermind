import base64
from datetime import timedelta
from decimal import Decimal

import yaml
from ddt import data, ddt
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.enums import ChecklistTypes, QuestionTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.media import models as media_models
from waldur_core.permissions.fixtures import CallRole
from waldur_mastermind.proposal import call_transfer, models, serializers
from waldur_mastermind.proposal.enums import (
    CallStates,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    RequestedOfferingStates,
)
from waldur_mastermind.proposal.tests import fixtures

from . import factories


def _without_document_filenames(export_data):
    # Storage may rename an uploaded file to avoid a collision, so the round
    # trip compares document content and description, not the stored name.
    for document in export_data.get("documents", []):
        document.pop("filename", None)
    return export_data


def _keys(value):
    if isinstance(value, dict):
        return set(value).union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value))
    return set()


@ddt
class CallTransferTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.add_user(self.fixture.call_manager, CallRole.MANAGER)
        self.call.description = "Call for compute time"
        self.call.fixed_duration_in_days = 365
        self.call.proposal_slug_template = "{call_slug}-{round_slug}-{seq}"
        self.call.backend_id = "HPC-2026"
        self.call.compliance_checklist = checklist_factories.ChecklistFactory(
            name="Export control", checklist_type=ChecklistTypes.PROPOSAL_COMPLIANCE
        )
        self.call.save()

        first_round = self.fixture.round
        factories.RoundFactory(
            call=self.call,
            start_time=first_round.cutoff_time + timedelta(days=1),
            cutoff_time=first_round.cutoff_time + timedelta(days=30),
        )
        self.requested_offering = factories.RequestedOfferingFactory(call=self.call)
        factories.CallResourceTemplateFactory(
            call=self.call, requested_offering=self.requested_offering
        )
        # Replace the mappings a new call is seeded with.
        self.call.proposalprojectrolemapping_set.all().delete()
        factories.ProposalProjectRoleMappingFactory(call=self.call)

        document = models.CallDocument(call=self.call, description="Guidelines")
        document.file.save("guidelines.pdf", ContentFile(b"%PDF-1.4 guidelines"))
        self.call.documents.add(document)

        step = self.call.workflow_steps.get(key="administrative_check")
        step.duration_in_days = 5
        step.save()
        step.criteria.create(name="Eligibility", order=1)
        step.notification_rules.all().delete()
        step.notification_rules.create(
            trigger=NotificationRuleTriggers.DEADLINE_APPROACHING,
            recipient=NotificationRuleRecipients.RESPONSIBLE_ROLE,
            days_before=2,
        )

        models.CallCOIConfiguration.objects.update_or_create(
            call=self.call, defaults={"coauthorship_lookback_years": 7}
        )

    def export(self, user="staff", call=None, **flags):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CallFactory.get_protected_url(call or self.call, "export_call")
        return self.client.post(url, flags, format="json")

    def import_(self, call_data, user="staff", manager=None, **params):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CallFactory.get_protected_list_url("import_call")
        payload = {
            "manager": (manager or self.fixture.manager).uuid.hex,
            "call_data": call_data,
            **params,
        }
        return self.client.post(url, payload, format="json")

    @data("staff", "call_manager", "call_organizer_user")
    def test_user_can_export_call(self, user):
        response = self.export(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["call_name"], self.call.name)

    def test_owner_sees_but_can_not_export_call(self):
        response = self.export("owner")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unrelated_user_can_not_export_call(self):
        response = self.export("user")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_export_carries_no_identifiers_or_people(self):
        factories.ProposalFactory(round=self.call.round_set.first())
        export_data = self.export().data["export_data"]

        dumped = yaml.safe_dump(export_data)
        for identifier in (
            self.call.uuid.hex,
            self.call.created_by.uuid.hex,
            self.requested_offering.offering.uuid.hex,
        ):
            self.assertNotIn(identifier, dumped)
        self.assertFalse(
            _keys(export_data)
            & {"uuid", "created_by", "approved_by", "panel_chair", "order_author_user"}
        )
        self.assertNotIn("proposals", export_data)
        self.assertIn(
            {
                "name": self.requested_offering.offering.name,
                "provider_name": self.requested_offering.offering.customer.name,
            },
            [ro["offering"] for ro in export_data["requested_offerings"]],
        )
        self.assertEqual(
            base64.b64decode(export_data["documents"][0]["content"]),
            b"%PDF-1.4 guidelines",
        )

    def test_round_trip_through_yaml(self):
        exported = self.export().data["export_data"]

        response = self.import_(yaml.safe_dump(exported))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["warnings"], [])

        new_call = models.Call.objects.get(uuid=response.data["call_uuid"])
        self.assertNotEqual(new_call.pk, self.call.pk)
        self.assertEqual(new_call.state, CallStates.DRAFT)
        self.assertEqual(new_call.created_by, self.fixture.staff)
        self.assertEqual(new_call.compliance_checklist, self.call.compliance_checklist)
        requested = new_call.requestedoffering_set.get(
            offering=self.requested_offering.offering,
            plan__name=self.requested_offering.plan.name,
        )
        self.assertEqual(requested.state, RequestedOfferingStates.REQUESTED)
        self.assertEqual(requested.plan, self.requested_offering.plan)

        reexported = self.export(call=new_call).data["export_data"]
        self.assertEqual(
            _without_document_filenames(reexported),
            _without_document_filenames(exported),
        )

    def test_contribution_settings_travel_with_the_call(self):
        step = self.call.workflow_steps.get(key="technical_assessment")
        step.contributor_source = "requested_offering_team"
        step.per_requested_offering = True
        step.outcome_set = "per_offering"
        step.on_negative = "continue"
        step.min_contributions = 1
        step.allows_conditional_acceptance = False
        step.save()
        exported = self.export().data["export_data"]

        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        copied = models.CallWorkflowStep.objects.get(
            call__uuid=response.data["call_uuid"], key="technical_assessment"
        )
        for field in (
            "contributor_source",
            "per_requested_offering",
            "outcome_set",
            "on_negative",
            "min_contributions",
            "allows_conditional_acceptance",
        ):
            self.assertEqual(getattr(copied, field), getattr(step, field), field)

    def test_min_reviewers_of_an_older_export_is_read(self):
        exported = self.export().data["export_data"]
        for step in exported["workflow_steps"]:
            if step["key"] == "expert_review":
                step["min_reviewers"] = step.pop("min_contributions")
                step["min_reviewers"] = 2
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["warnings"], [])
        copied = models.CallWorkflowStep.objects.get(
            call__uuid=response.data["call_uuid"], key="expert_review"
        )
        self.assertEqual(copied.min_contributions, 2)

    def test_import_can_rename_call(self):
        exported = self.export().data["export_data"]
        response = self.import_(exported, name="Autumn call")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["call_name"], "Autumn call")

    def test_unticked_section_is_left_out_of_export(self):
        export_data = self.export(include_rounds=False, include_offerings=False).data[
            "export_data"
        ]
        self.assertNotIn("rounds", export_data)
        self.assertNotIn("requested_offerings", export_data)
        self.assertIn("workflow_steps", export_data)

    def test_unticked_section_is_ignored_on_import(self):
        exported = self.export().data["export_data"]
        response = self.import_(
            exported, import_rounds=False, import_workflow_steps=False
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotIn("rounds", response.data["imported_sections"])
        new_call = models.Call.objects.get(uuid=response.data["call_uuid"])
        self.assertEqual(new_call.round_set.count(), 0)
        # Only the steps seeded on creation, with their default settings.
        step = new_call.workflow_steps.get(key="administrative_check")
        self.assertNotEqual(step.duration_in_days, 5)
        self.assertFalse(step.criteria.exists())

    def test_unresolved_offering_is_skipped_with_warning(self):
        exported = self.export().data["export_data"]
        for requested in exported["requested_offerings"]:
            requested["offering"]["name"] = "No such offering"

        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            len(response.data["warnings"]), len(exported["requested_offerings"])
        )
        for warning in response.data["warnings"]:
            self.assertIn("No such offering", warning)
        new_call = models.Call.objects.get(uuid=response.data["call_uuid"])
        self.assertFalse(new_call.requestedoffering_set.exists())
        self.assertEqual(new_call.round_set.count(), self.call.round_set.count())

    def test_ambiguous_checklist_is_skipped_with_warning(self):
        checklist_factories.ChecklistFactory(
            name="Export control", checklist_type=ChecklistTypes.PROPOSAL_COMPLIANCE
        )
        response = self.import_(self.export().data["export_data"])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("more than one", response.data["warnings"][0])
        new_call = models.Call.objects.get(uuid=response.data["call_uuid"])
        self.assertIsNone(new_call.compliance_checklist)

    def test_unknown_field_is_ignored_with_warning(self):
        exported = self.export().data["export_data"]
        exported["call"]["field_from_newer_portal"] = True
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("field_from_newer_portal", response.data["warnings"][0])

    def test_the_tracks_of_an_older_document_are_ignored_with_a_warning(self):
        exported = self.export().data["export_data"]
        self.assertNotIn("tracks", exported)
        exported["tracks"] = [
            {"key": "industry", "name": "Industry", "description": "", "order": 0}
        ]
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            any("tracks" in warning for warning in response.data["warnings"]),
            response.data["warnings"],
        )

    def test_call_organizer_can_import_into_own_organisation(self):
        response = self.import_(
            self.export().data["export_data"], user="call_organizer_user"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data("call_manager", "owner", "user")
    def test_user_without_create_call_can_not_import(self, user):
        response = self.import_(self.export().data["export_data"], user=user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_malformed_yaml_is_rejected(self):
        response = self.import_("call: [unclosed")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_yaml_alias_expansion_is_rejected_before_it_expands(self):
        # Nine levels of nine aliases each: a few hundred bytes that expand
        # to 9**9 nodes once loaded and stored as JSON.
        lines = ['a0: &a0 ["x"]']
        for level in range(1, 10):
            refs = ", ".join([f"*a{level - 1}"] * 9)
            lines.append(f"a{level}: &a{level} [{refs}]")
        bomb = (
            "\n".join(lines) + "\ncoi_configuration:\n  recusal_required_types: *a9\n"
        )
        self.assertLess(len(bomb), 1024)
        calls_before = models.Call.objects.count()
        started = timezone.now()
        response = self.import_(bomb)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("aliases are not supported", str(response.data["call_data"]))
        self.assertLess(timezone.now() - started, timedelta(seconds=5))
        self.assertEqual(models.Call.objects.count(), calls_before)

    def test_a_yaml_anchor_alone_is_rejected(self):
        response = self.import_("schema_version: &v 1\n")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("call_data", response.data)

    def test_an_oversized_call_document_is_rejected(self):
        exported = self.export().data["export_data"]
        exported["call"]["description"] = "x" * serializers.CALL_DATA_MAX_BYTES
        response = self.import_(yaml.safe_dump(exported))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("larger than", str(response.data["call_data"]))
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("larger than", str(response.data["call_data"]))

    def test_an_export_round_trips_through_the_alias_free_loader(self):
        exported = self.export().data["export_data"]
        self.assertEqual(
            yaml.load(
                yaml.safe_dump(exported),
                Loader=serializers.NoAliasSafeLoader,  # noqa: S506
            ),
            yaml.safe_load(yaml.safe_dump(exported)),
        )

    def test_unknown_schema_version_is_rejected(self):
        exported = self.export().data["export_data"]
        exported["schema_version"] = 99
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_value_rolls_back_whole_import(self):
        exported = self.export().data["export_data"]
        exported["rounds"][0]["start_time"] = "not a date"
        calls_before = models.Call.objects.count()

        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.Call.objects.count(), calls_before)

    def test_missing_required_field_is_rejected(self):
        exported = self.export().data["export_data"]
        del exported["rounds"][0]["start_time"]
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("rounds[0]", response.data)

    @data(
        ("compliance_checklist", "Export control"),
        ("rounds", {"start_time": "2026-01-01T00:00:00Z"}),
        ("requested_offerings", ["not a mapping"]),
    )
    def test_malformed_reference_is_rejected(self, case):
        key, value = case
        exported = self.export().data["export_data"]
        exported[key] = value
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_criterion_is_rejected(self):
        exported = self.export().data["export_data"]
        step = next(
            s for s in exported["workflow_steps"] if s["key"] == "administrative_check"
        )
        step["criteria"].append(dict(step["criteria"][0]))
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_model_rules_are_enforced(self):
        exported = self.export().data["export_data"]
        exported["matching_configuration"] = {
            "keyword_weight": 0.9,
            "text_weight": 0.5,
        }
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("matching_configuration", response.data)

    def test_round_cutoff_before_start_is_rejected(self):
        exported = self.export().data["export_data"]
        exported["rounds"][0]["cutoff_time"] = "2020-01-01T00:00:00+00:00"
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unreadable_document_is_left_out_of_export_with_warning(self):
        document = self.call.documents.get()
        default_storage.delete(document.file.name)

        response = self.export()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["export_data"]["documents"], [])
        self.assertEqual(len(response.data["warnings"]), 1)
        self.assertIn("guidelines", response.data["warnings"][0])

    def test_failed_import_leaves_no_stored_file(self):
        exported = self.export().data["export_data"]
        exported["documents"].append({"filename": "x.pdf", "content": "not base64!"})
        stored_before = media_models.File.objects.count()

        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(media_models.File.objects.count(), stored_before)

    def test_skipped_sections_are_not_reported_as_imported(self):
        exported = self.export().data["export_data"]
        exported["compliance_checklist"]["name"] = "No such checklist"
        for requested in exported["requested_offerings"]:
            requested["offering"]["provider_name"] = "No such provider"

        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotIn("compliance_checklist", response.data["imported_sections"])
        self.assertNotIn("offerings", response.data["imported_sections"])
        self.assertIn("rounds", response.data["imported_sections"])

    def test_reference_code_travels_with_the_call(self):
        exported = self.export().data["export_data"]
        self.assertEqual(exported["call"]["reference_code"], "HPC-2026")
        self.assertNotIn("backend_id", exported["call"])

        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["warnings"], [])
        new_call = models.Call.objects.get(uuid=response.data["call_uuid"])
        self.assertEqual(new_call.backend_id, "HPC-2026")

    def test_overlong_reference_code_is_rejected(self):
        exported = self.export().data["export_data"]
        exported["call"]["reference_code"] = "x" * 1000
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("call.reference_code", response.data)


class CallTransferSectionFlagsTest(test.APITestCase):
    def test_every_section_has_an_export_and_an_import_flag(self):
        export_flags = {
            name.removeprefix("include_")
            for name in serializers.CallExportParametersSerializer().fields
        }
        import_flags = {
            name.removeprefix("import_")
            for name in serializers.CallImportParametersSerializer().fields
            if name.startswith("import_")
        }
        self.assertEqual(export_flags, set(call_transfer.SECTIONS))
        self.assertEqual(import_flags, set(call_transfer.SECTIONS))


@ddt
class CallTransferFormsTest(test.APITestCase):
    """The evaluation forms of the steps travel with the call."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.form = checklist_factories.ChecklistFactory(
            name="Panel report", checklist_type=ChecklistTypes.WORKFLOW_STEP
        )
        self.verdict = checklist_factories.QuestionFactory(
            checklist=self.form,
            order=1,
            description="Verdict",
            question_type=QuestionTypes.SINGLE_SELECT,
            required=True,
            operator="in",
            review_answer_value=[],
        )
        self.options = [
            checklist_factories.QuestionOptionFactory(
                question=self.verdict, label=label, order=index
            )
            for index, label in enumerate(["Accept", "Revise", "Decline"])
        ]
        # A review trigger names an option, as an answer does.
        self.verdict.review_answer_value = [self.options[2].uuid.hex]
        self.verdict.save()
        self.revisions = checklist_factories.QuestionFactory(
            checklist=self.form,
            order=2,
            description="Revisions needed",
            question_type=QuestionTypes.RICH_TEXT,
            operator="",
            rich_text_char_limit=2000,
            rich_text_toolbar_level="minimal",
        )
        checklist_factories.QuestionDependencyFactory(
            question=self.revisions,
            depends_on_question=self.verdict,
            required_answer_value=[self.options[1].uuid.hex],
            operator="in",
        )
        checklist_factories.QuestionFactory(
            checklist=self.form,
            order=3,
            description="Score",
            question_type=QuestionTypes.RATING,
            operator="",
            min_value=1,
            max_value=5,
        )
        checklist_factories.QuestionFactory(
            checklist=self.form,
            order=4,
            description="Annex",
            question_type=QuestionTypes.FILE,
            operator="",
            allowed_file_types=[".pdf"],
            max_file_size_mb=5,
        )
        self.step = self.call.workflow_steps.get(key="administrative_check")
        self.step.checklist = self.form
        self.step.save()

    def export(self, call=None):
        data, warnings = call_transfer.export_call(call or self.call)
        self.assertEqual(warnings, [])
        return data

    def import_(self, call_data):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(
            factories.CallFactory.get_protected_list_url("import_call"),
            {"manager": self.fixture.manager.uuid.hex, "call_data": call_data},
            format="json",
        )

    def imported_call(self, call_data, warnings=()):
        response = self.import_(call_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["warnings"], list(warnings))
        return models.Call.objects.get(uuid=response.data["call_uuid"])

    def test_export_carries_the_form_definition(self):
        exported = self.export()
        self.assertEqual(exported["schema_version"], call_transfer.SCHEMA_VERSION)
        step = next(
            s for s in exported["workflow_steps"] if s["key"] == "administrative_check"
        )
        self.assertEqual(
            step["checklist"],
            {"name": "Panel report", "checklist_type": ChecklistTypes.WORKFLOW_STEP},
        )
        [form] = exported["checklists"]
        self.assertEqual(form["name"], "Panel report")
        verdict, revisions, score, annex = form["questions"]
        self.assertEqual(
            [o["label"] for o in verdict["options"]], ["Accept", "Revise", "Decline"]
        )
        # Options are named by label, never by their identifiers here.
        self.assertEqual(verdict["review_answer_value"], ["Decline"])
        self.assertEqual(
            revisions["dependencies"],
            [{"depends_on": 0, "operator": "in", "required_answer_value": ["Revise"]}],
        )
        self.assertEqual(revisions["rich_text_char_limit"], 2000)
        self.assertEqual(
            (Decimal(score["min_value"]), Decimal(score["max_value"])), (1, 5)
        )
        self.assertEqual(annex["allowed_file_types"], [".pdf"])
        self.assertFalse(_keys(form) & {"uuid", "checklist", "question"})
        dumped = yaml.safe_dump(exported)
        for option in self.options:
            self.assertNotIn(option.uuid.hex, dumped)

    def test_round_trip_gives_the_call_its_own_form(self):
        exported = self.export()
        new_call = self.imported_call(yaml.safe_dump(exported))

        form = new_call.workflow_steps.get(key="administrative_check").checklist
        self.assertNotEqual(form, self.form)
        self.assertEqual(form.checklist_type, ChecklistTypes.WORKFLOW_STEP)
        verdict, revisions, score, annex = form.questions.order_by("order")
        self.assertEqual(
            list(verdict.question_options.values_list("label", flat=True)),
            ["Accept", "Revise", "Decline"],
        )
        decline = verdict.question_options.get(label="Decline")
        self.assertEqual(verdict.review_answer_value, [decline.uuid.hex])
        dependency = revisions.dependencies.get()
        self.assertEqual(dependency.depends_on_question, verdict)
        self.assertEqual(
            dependency.required_answer_value,
            [verdict.question_options.get(label="Revise").uuid.hex],
        )
        self.assertEqual(revisions.rich_text_toolbar_level, "minimal")
        self.assertEqual((score.min_value, score.max_value), (1, 5))
        self.assertEqual(annex.max_file_size_mb, 5)

        # Exported again, the copy reads as the original, under its own name.
        reexported = self.export(new_call)
        for document in (exported, reexported):
            for step in document["workflow_steps"]:
                if step["checklist"]:
                    step["checklist"]["name"] = "*"
            for checklist in document["checklists"]:
                checklist["name"] = "*"
        self.assertEqual(reexported["checklists"], exported["checklists"])

    def test_editing_the_copy_leaves_the_original_alone(self):
        new_call = self.imported_call(self.export())
        form = new_call.workflow_steps.get(key="administrative_check").checklist
        form.questions.filter(description="Score").delete()
        self.assertTrue(self.form.questions.filter(description="Score").exists())

    def test_form_name_clash_adds_the_call_name(self):
        exported = self.export()
        # The original keeps its name; a copy can not take it.
        first = self.imported_call(exported)
        second = self.imported_call(exported)
        first_form = first.workflow_steps.get(key="administrative_check").checklist
        second_form = second.workflow_steps.get(key="administrative_check").checklist
        self.assertEqual(first_form.name, f"Panel report ({first.name})")
        self.assertEqual(second_form.name, f"Panel report ({second.name}, 2)")

    def test_a_copy_exports_under_the_form_name(self):
        copy = self.imported_call(self.export())
        form = copy.workflow_steps.get(key="administrative_check").checklist
        self.assertEqual(form.name, f"Panel report ({copy.name})")
        exported = self.export(copy)
        [definition] = exported["checklists"]
        self.assertEqual(definition["name"], "Panel report")
        step = next(
            s for s in exported["workflow_steps"] if s["key"] == "administrative_check"
        )
        self.assertEqual(step["checklist"]["name"], "Panel report")
        # Imported again, the copy of the copy gains one call name, not two.
        again = self.imported_call(exported)
        form = again.workflow_steps.get(key="administrative_check").checklist
        self.assertEqual(form.name, f"Panel report ({again.name}, 2)")

    def test_unused_form_name_is_kept(self):
        exported = self.export()
        self.step.checklist = None
        self.step.save()
        self.form.delete()
        new_call = self.imported_call(exported)
        form = new_call.workflow_steps.get(key="administrative_check").checklist
        self.assertEqual(form.name, "Panel report")

    def test_steps_sharing_a_form_share_the_copy(self):
        other = self.call.workflow_steps.get(key="technical_assessment")
        other.checklist = self.form
        other.save()
        exported = self.export()
        self.assertEqual(len(exported["checklists"]), 1)
        new_call = self.imported_call(exported)
        forms = set(
            new_call.workflow_steps.filter(
                key__in=["administrative_check", "technical_assessment"]
            ).values_list("checklist", flat=True)
        )
        self.assertEqual(len(forms), 1)
        self.assertNotIn(self.form.pk, forms)

    @data(1, 2)
    def test_older_document_binds_the_form_by_name(self, version):
        exported = self.export()
        exported["schema_version"] = version
        del exported["checklists"]
        checklists_before = checklist_models.Checklist.objects.count()

        new_call = self.imported_call(exported)
        self.assertEqual(
            new_call.workflow_steps.get(key="administrative_check").checklist,
            self.form,
        )
        self.assertEqual(checklist_models.Checklist.objects.count(), checklists_before)

    def test_older_document_naming_an_unknown_form_is_imported_without_it(self):
        exported = self.export()
        del exported["checklists"]
        self.form.name = "Renamed"
        self.form.save()
        new_call = self.imported_call(
            exported,
            warnings=[
                "Workflow step checklist 'Panel report' was not found on "
                "this portal, skipped."
            ],
        )
        self.assertIsNone(
            new_call.workflow_steps.get(key="administrative_check").checklist
        )

    def test_form_no_step_uses_is_skipped_with_warning(self):
        exported = self.export()
        exported["checklists"].append(
            {**exported["checklists"][0], "name": "Spare form"}
        )
        checklists_before = checklist_models.Checklist.objects.count()
        self.imported_call(
            exported,
            warnings=["Form 'Spare form' is used by no workflow step, skipped."],
        )
        self.assertEqual(
            checklist_models.Checklist.objects.count(), checklists_before + 1
        )

    def test_question_types_and_options_are_checked(self):
        cases = [
            # Not a question type.
            ({"question_type": "essay"}, "checklists[0].questions[0]"),
            # Names an option the question does not have.
            (
                {"review_answer_value": ["Maybe"]},
                "checklists[0].questions[0].review_answer_value",
            ),
            # A field of another question type.
            ({"rich_text_char_limit": 10}, "checklists[0].questions[0]"),
        ]
        for change, key in cases:
            with self.subTest(change=change):
                exported = self.export()
                exported["checklists"][0]["questions"][0].update(change)
                response = self.import_(exported)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(key, response.data)

    def test_dependency_must_name_a_question_and_an_option(self):
        cases = [
            ({"depends_on": 9}, "depends_on"),
            ({"depends_on": 1}, ""),
            ({"required_answer_value": ["Maybe"]}, "required_answer_value"),
        ]
        for change, field in cases:
            with self.subTest(change=change):
                exported = self.export()
                exported["checklists"][0]["questions"][1]["dependencies"][0].update(
                    change
                )
                response = self.import_(exported)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                key = "checklists[0].questions[1].dependencies[0]"
                self.assertIn(f"{key}.{field}" if field else key, response.data)

    def test_only_workflow_step_forms_can_be_defined(self):
        exported = self.export()
        exported["checklists"][0]["checklist_type"] = ChecklistTypes.PROJECT_COMPLIANCE
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("checklists[0].checklist_type", response.data)

    def test_failed_import_leaves_no_form(self):
        exported = self.export()
        exported["checklists"][0]["questions"][3]["question_type"] = "essay"
        checklists_before = checklist_models.Checklist.objects.count()
        response = self.import_(exported)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(checklist_models.Checklist.objects.count(), checklists_before)

    def test_review_operator_without_a_value_does_not_block_import(self):
        # Forms made outside the checklist admin API may keep an operator
        # with nothing to compare against; it triggers nothing either way.
        checklist_factories.QuestionFactory(
            checklist=self.form, order=5, description="Notes", operator="contains"
        )
        new_call = self.imported_call(self.export())
        form = new_call.workflow_steps.get(key="administrative_check").checklist
        self.assertTrue(form.questions.filter(description="Notes").exists())
