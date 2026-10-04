import base64
from datetime import timedelta

import yaml
from ddt import data, ddt
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from rest_framework import status, test

from waldur_core.checklist.enums import ChecklistTypes
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

        step = self.call.workflow_steps.get(step="administrative_check")
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
        step = new_call.workflow_steps.get(step="administrative_check")
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
            s for s in exported["workflow_steps"] if s["step"] == "administrative_check"
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
