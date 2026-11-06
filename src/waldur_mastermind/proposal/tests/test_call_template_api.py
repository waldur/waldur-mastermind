"""Staff-managed call templates and creating a call from one."""

from unittest import mock

import yaml
from ddt import data, ddt
from django.urls import reverse
from rest_framework import serializers as drf_serializers
from rest_framework import status, test
from rest_framework.validators import UniqueValidator

from waldur_core.checklist import models as checklist_models
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_templates,
    call_transfer,
    models,
    serializers,
)
from waldur_mastermind.proposal.enums import CallStates
from waldur_mastermind.proposal.tests import factories, fixtures

TEMPLATE = "extreme_scale"


def list_url():
    return "http://testserver" + reverse("call-template-list")


def presets_url(action="presets"):
    return "http://testserver" + reverse(f"call-template-{action.replace('_', '-')}")


def detail_url(template, action=None):
    url = "http://testserver" + reverse(
        "call-template-detail", kwargs={"uuid": template.uuid.hex}
    )
    return url if action is None else url + action + "/"


def make_template(name="Template", is_published=True, document=None):
    document = document or call_templates.load(TEMPLATE)
    return models.CallTemplate.objects.create(
        name=name,
        document=document,
        schema_version=document["schema_version"],
        is_published=is_published,
    )


class CallTemplateTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.organizer = self.fixture.call_organizer_user

    def user(self, name):
        return self.organizer if name == "organizer" else getattr(self.fixture, name)


@ddt
class CallTemplateAuthoringTest(CallTemplateTestBase):
    def create(self, document, user="staff", **payload):
        self.client.force_authenticate(self.user(user))
        return self.client.post(
            list_url(),
            {"name": "Access call", "document": document, **payload},
            format="json",
        )

    def test_staff_creates_an_unpublished_template(self):
        response = self.create(call_templates.load(TEMPLATE))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["warnings"], [])
        self.assertFalse(response.data["is_published"])
        self.assertEqual(response.data["schema_version"], 3)
        self.assertEqual(str(response.data["created_by"]), self.fixture.staff.uuid.hex)
        self.assertEqual(response.data["steps"][0]["name"], "Administrative check")

    def test_a_template_is_accepted_as_yaml(self):
        response = self.create(yaml.safe_dump(call_templates.load(TEMPLATE)))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        template = models.CallTemplate.objects.get(uuid=response.data["uuid"])
        self.assertEqual(template.document, call_templates.load(TEMPLATE))

    def test_checking_a_template_leaves_nothing_behind(self):
        counts = (
            structure_models.Customer.objects.count(),
            models.CallManagingOrganisation.objects.count(),
            models.Call.objects.count(),
            checklist_models.Checklist.objects.count(),
        )
        response = self.create(call_templates.load(TEMPLATE))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            counts,
            (
                structure_models.Customer.objects.count(),
                models.CallManagingOrganisation.objects.count(),
                models.Call.objects.count(),
                checklist_models.Checklist.objects.count(),
            ),
        )

    @data("rounds", "requested_offerings", "documents")
    def test_a_template_carries_no_call_specific_section(self, key):
        document = call_templates.load(TEMPLATE)
        document[key] = [{"name": "anything"}]
        response = self.create(document)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(key, str(response.data["document"]))

    def test_an_empty_call_specific_section_is_harmless(self):
        document = call_templates.load(TEMPLATE)
        document["rounds"] = []
        response = self.create(document)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_unsupported_schema_version_is_refused(self):
        document = call_templates.load(TEMPLATE)
        document["schema_version"] = 99
        response = self.create(document)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_oversized_document_is_refused(self):
        document = call_templates.load(TEMPLATE)
        document["call"]["description"] = "x" * serializers.CALL_DATA_MAX_BYTES
        response = self.create(document)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("larger than", str(response.data["document"]))

    def test_yaml_aliases_are_refused(self):
        response = self.create("schema_version: &v 3\ncall: {name: *v}\n")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_document_that_does_not_import_is_refused(self):
        document = call_templates.load(TEMPLATE)
        document["workflow_steps"][0]["kind"] = "no such kind"
        calls = models.Call.objects.count()
        response = self.create(document)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("document", response.data)
        self.assertEqual(models.Call.objects.count(), calls)
        self.assertFalse(models.CallTemplate.objects.exists())

    def test_import_warnings_are_returned(self):
        document = call_templates.load(TEMPLATE)
        document["call"]["field_from_newer_portal"] = True
        response = self.create(document)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("field_from_newer_portal", response.data["warnings"][0])

    def test_staff_edits_and_publishes(self):
        template = make_template(is_published=False)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            detail_url(template),
            {"name": "Renamed", "is_published": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        template.refresh_from_db()
        self.assertEqual(template.name, "Renamed")
        self.assertTrue(template.is_published)

    def test_staff_deletes(self):
        template = make_template()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(detail_url(template))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.CallTemplate.objects.exists())

    @data("organizer", "owner", "user")
    def test_only_staff_authors(self, user):
        template = make_template()
        response = self.create(call_templates.load(TEMPLATE), user=user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        for method in ("patch", "delete"):
            response = getattr(self.client, method)(
                detail_url(template), {"name": "Mine"}, format="json"
            )
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )
        template.refresh_from_db()
        self.assertEqual(template.name, "Template")


@ddt
class CallTemplateVisibilityTest(CallTemplateTestBase):
    def setUp(self):
        super().setUp()
        self.published = make_template("Published", is_published=True)
        self.draft = make_template("Draft", is_published=False)

    def list_names(self, user):
        self.client.force_authenticate(self.user(user))
        response = self.client.get(list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [item["name"] for item in response.data]

    def test_staff_sees_every_template(self):
        self.assertEqual(self.list_names("staff"), ["Draft", "Published"])

    def test_call_creator_sees_published_templates_without_the_document(self):
        self.assertEqual(self.list_names("organizer"), ["Published"])
        response = self.client.get(detail_url(self.published))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(response.data), set(serializers.CALL_TEMPLATE_PUBLIC_FIELDS)
        )
        self.assertEqual(len(response.data["steps"]), 18)
        response = self.client.get(detail_url(self.draft))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("owner", "user")
    def test_who_can_not_create_calls_sees_no_template(self, user):
        self.assertEqual(self.list_names(user), [])

    def test_create_call_held_on_the_customer_alone_does_not_count(self):
        # import_call checks CREATE_CALL on the call managing organisation
        # itself; listing templates follows the same rule.
        holder = structure_factories.UserFactory()
        self.fixture.customer.add_user(holder, self.fixture.call_organizer_role)
        self.client.force_authenticate(holder)
        response = self.client.get(list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])
        response = self.client.post(
            factories.CallFactory.get_protected_list_url("import_call"),
            {
                "manager": self.fixture.manager.uuid.hex,
                "template": self.published.uuid.hex,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# Validation sees the name free; a concurrent save takes it before this one.
skip_unique_check = mock.patch.object(UniqueValidator, "__call__", return_value=None)


class CallTemplateNameRaceTest(CallTemplateTestBase):
    def setUp(self):
        super().setUp()
        make_template(name="Taken")
        self.client.force_authenticate(self.fixture.staff)

    def assert_name_taken(self, response):
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            [str(e) for e in response.data["name"]], [call_templates.NAME_TAKEN]
        )

    @skip_unique_check
    def test_create(self, _):
        response = self.client.post(
            list_url(),
            {"name": "Taken", "document": call_templates.load(TEMPLATE)},
            format="json",
        )
        self.assert_name_taken(response)
        self.assertEqual(models.CallTemplate.objects.count(), 1)

    @skip_unique_check
    def test_update(self, _):
        other = make_template(name="Other")
        response = self.client.patch(
            detail_url(other), {"name": "Taken"}, format="json"
        )
        self.assert_name_taken(response)

    @skip_unique_check
    def test_save_as_template(self, _):
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.fixture.call, "save_as_template"
            ),
            {"name": "Taken"},
            format="json",
        )
        self.assert_name_taken(response)


@ddt
class CallFromTemplateTest(CallTemplateTestBase):
    def setUp(self):
        super().setUp()
        self.template = make_template(is_published=True)

    def import_(self, user="organizer", **payload):
        self.client.force_authenticate(self.user(user))
        return self.client.post(
            factories.CallFactory.get_protected_list_url("import_call"),
            {"manager": self.fixture.manager.uuid.hex, **payload},
            format="json",
        )

    def test_call_creator_creates_a_draft_call_from_a_template(self):
        response = self.import_(template=self.template.uuid.hex, name="Spring call")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["warnings"], [])
        call = models.Call.objects.get(uuid=response.data["call_uuid"])
        self.assertEqual(call.name, "Spring call")
        self.assertEqual(call.state, CallStates.DRAFT)
        self.assertEqual(call.manager, self.fixture.manager)
        self.assertEqual(call.workflow_steps.count(), 18)

    def test_unpublished_template_is_not_found_by_call_creator(self):
        self.template.is_published = False
        self.template.save()
        response = self.import_(template=self.template.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("template", response.data)

    def test_staff_creates_a_call_from_an_unpublished_template(self):
        self.template.is_published = False
        self.template.save()
        response = self.import_(user="staff", template=self.template.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data("call_manager", "owner", "user")
    def test_who_can_not_create_calls_can_not_use_a_template(self, user):
        response = self.import_(user=user, template=self.template.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_template_and_call_data_are_exclusive(self):
        response = self.import_(
            template=self.template.uuid.hex,
            call_data=call_templates.load(TEMPLATE),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.import_()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_template_never_brings_rounds_offerings_or_documents(self):
        # A row written past the API still cannot bring them.
        document = call_templates.load(TEMPLATE)
        document["rounds"] = [{"start_time": "2030-01-01T00:00:00Z"}]
        self.template.document = document
        self.template.save()
        response = self.import_(template=self.template.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        call = models.Call.objects.get(uuid=response.data["call_uuid"])
        self.assertFalse(call.round_set.exists())

    def test_upload_still_works(self):
        response = self.import_(call_data=call_templates.load(TEMPLATE))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


@ddt
class SaveCallAsTemplateTest(CallTemplateTestBase):
    def setUp(self):
        super().setUp()
        self.call = self.fixture.call
        self.call.add_user(self.fixture.call_manager, CallRole.MANAGER)
        factories.RequestedOfferingFactory(call=self.call)

    def save_as_template(self, user="staff", **payload):
        self.client.force_authenticate(self.user(user))
        return self.client.post(
            factories.CallFactory.get_protected_url(self.call, "save_as_template"),
            payload,
            format="json",
        )

    def test_staff_saves_a_call_as_an_unpublished_template(self):
        response = self.save_as_template(name="From a call")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        template = models.CallTemplate.objects.get(uuid=response.data["uuid"])
        self.assertEqual(template.name, "From a call")
        self.assertFalse(template.is_published)
        self.assertEqual(template.created_by, self.fixture.staff)
        for key in call_templates.EXCLUDED_KEYS:
            self.assertNotIn(key, template.document)
        self.assertIn("workflow_steps", template.document)

    def test_template_name_defaults_to_the_call_name(self):
        response = self.save_as_template()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["name"], self.call.name)

    def test_template_names_are_unique(self):
        make_template(name="Taken")
        response = self.save_as_template(name="Taken")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("call_manager", "organizer")
    def test_only_staff_saves_a_call_as_template(self, user):
        response = self.save_as_template(user=user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.CallTemplate.objects.exists())


@ddt
class CallTemplatePresetsTest(CallTemplateTestBase):
    def presets(self, user="staff"):
        self.client.force_authenticate(self.user(user))
        return self.client.get(presets_url())

    def load(self, presets, user="staff", **payload):
        self.client.force_authenticate(self.user(user))
        return self.client.post(
            presets_url("load_preset"),
            {"presets": presets, **payload},
            format="json",
        )

    def test_a_fresh_portal_has_no_call_templates(self):
        self.assertFalse(models.CallTemplate.objects.exists())

    def test_staff_lists_the_shipped_presets(self):
        response = self.presets()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([p["key"] for p in response.data], call_templates.names())
        (preset,) = [p for p in response.data if p["key"] == TEMPLATE]
        document = call_templates.load(TEMPLATE)
        self.assertEqual(preset["name"], "Extreme Scale Access")
        self.assertEqual(preset["description"], document["call"]["description"])
        self.assertEqual(preset["steps"], call_templates.step_summary(document))
        self.assertFalse(preset["name_taken"])

    def test_a_preset_whose_name_a_template_holds_is_flagged(self):
        make_template(name="Extreme Scale Access")
        flags = {p["key"]: p["name_taken"] for p in self.presets().data}
        self.assertTrue(flags[TEMPLATE])
        self.assertFalse(flags["regular_access"])

    @data("organizer", "owner", "user")
    def test_only_staff_lists_presets(self, user):
        self.assertEqual(self.presets(user).status_code, status.HTTP_403_FORBIDDEN)

    @data("organizer", "owner", "user")
    def test_only_staff_loads_presets(self, user):
        response = self.load([TEMPLATE], user=user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.CallTemplate.objects.exists())

    def test_load_creates_unpublished_templates(self):
        response = self.load([TEMPLATE, "regular_access"])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["created"], 2)
        self.assertIn("Extreme Scale Access", response.data["created_names"])
        templates = models.CallTemplate.objects.all()
        self.assertEqual(
            sorted(t.name for t in templates),
            ["Extreme Scale Access", "Regular Access"],
        )
        self.assertFalse(any(t.is_published for t in templates))
        template = templates.get(name="Extreme Scale Access")
        # A copy of the preset: its document, its preset block as metadata
        # and its scenarios.
        self.assertEqual(
            template.document,
            {
                **call_templates.load(TEMPLATE),
                "preset": call_templates.preset_block(TEMPLATE),
            },
        )
        self.assertEqual(template.scenarios, call_templates.load_scenarios(TEMPLATE))
        self.assertEqual(template.versions.count(), 1)

    def test_load_publishes_when_asked(self):
        self.load([TEMPLATE], publish=True)
        self.assertTrue(
            models.CallTemplate.objects.get(name="Extreme Scale Access").is_published
        )

    def test_a_name_clash_is_refused_and_names_the_template(self):
        existing = make_template(name="Extreme Scale Access")
        response = self.load(["regular_access", TEMPLATE])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Extreme Scale Access", str(response.data["presets"]))
        # Nothing is created, nor the existing template touched.
        self.assertEqual(list(models.CallTemplate.objects.all()), [existing])
        existing.refresh_from_db()
        self.assertEqual(existing.document, call_templates.load(TEMPLATE))

    def test_a_loaded_template_lives_its_own_life(self):
        self.load([TEMPLATE])
        template = models.CallTemplate.objects.get(name="Extreme Scale Access")
        template.name = "Our extreme scale"
        template.document = {**template.document, "call": {"name": "Edited"}}
        template.save()
        # Loading the preset again makes a second, independent template.
        response = self.load([TEMPLATE])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(models.CallTemplate.objects.count(), 2)
        template.refresh_from_db()
        self.assertEqual(template.document["call"], {"name": "Edited"})

    def test_unknown_preset_is_refused(self):
        response = self.load([TEMPLATE, "no_such_preset"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("presets", response.data)
        self.assertFalse(models.CallTemplate.objects.exists())

    def test_empty_selection_is_refused(self):
        response = self.load([])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_demo_loader_skips_a_name_already_held(self):
        make_template(name="Extreme Scale Access")
        result = call_templates.load_presets(
            [TEMPLATE, "regular_access"], skip_existing=True
        )
        self.assertEqual(result["created"], ["Regular Access"])
        self.assertEqual(result["skipped"], ["Extreme Scale Access"])


GOOD_PRESET = {
    "organization": "Example Research Council",
    "organization_url": "https://research.example.org",
    "process": "Open access call",
    "reference_url": "https://research.example.org/calls",
    "reference_title": "Open access calls",
    "category": "generic",
    "tags": ["HPC", "cut-off"],
    "summary": "Peer-reviewed allocations after each cut-off.",
}


@ddt
class CallTemplatePresetInfoTest(CallTemplateTestBase):
    """The ``preset`` block: which organisation and process a template models."""

    def test_every_shipped_preset_describes_its_process(self):
        for name in call_templates.names():
            block = call_templates.preset_block(name)
            self.assertIsNotNone(block, name)
            self.assertEqual(call_templates.check_preset(block), block)
            if block["category"] == "programme":
                self.assertIn("programme", block, name)
                self.assertTrue(block["reference_url"].startswith("https://"), name)
            else:
                self.assertNotIn("programme", block, name)

    def test_every_shipped_programme_is_well_formed(self):
        programmes = call_templates.programmes()
        self.assertTrue(programmes)
        for key, entry in programmes.items():
            self.assertEqual(call_templates.check_programme(key, entry), entry)
        # Every programme has a preset; the registry is no template.
        used = {
            call_templates.preset_block(name).get("programme")
            for name in call_templates.names()
        }
        self.assertEqual(set(programmes), used - {None})
        self.assertNotIn(
            call_templates.PROGRAMMES_FILE.removesuffix(".json"),
            call_templates.names(),
        )

    def test_the_eurohpc_presets_are_one_programme(self):
        eurohpc = {
            "ai_factory_fast_lane",
            "ai_factory_large_scale",
            "ai_factory_playground",
            "ai_for_science",
            "benchmark_access",
            "development_access",
            "extreme_scale",
            "quantum_access",
            "regular_access",
        }
        infos = {
            name: call_templates.preset_info(name) for name in call_templates.names()
        }
        self.assertEqual(
            {
                name
                for name, info in infos.items()
                if info["programme"] and info["programme"]["key"] == "eurohpc-ju-access"
            },
            eurohpc,
        )
        programme = infos["extreme_scale"]["programme"]
        self.assertEqual(programme["name"], "EuroHPC JU access calls")
        self.assertEqual(programme["organization"], "EuroHPC Joint Undertaking")
        # The process takes the programme's organisation.
        self.assertEqual(
            infos["extreme_scale"]["organization"], "EuroHPC Joint Undertaking"
        )

    def test_generic_presets_name_no_programme(self):
        infos = {
            name: call_templates.preset_info(name) for name in call_templates.names()
        }
        generic = [
            name for name, info in infos.items() if info["category"] == "generic"
        ]
        self.assertTrue(generic)
        for name in generic:
            self.assertIsNone(infos[name]["programme"], name)

    def test_the_template_document_leaves_the_block_out(self):
        self.assertNotIn(call_templates.PRESET_KEY, call_templates.load(TEMPLATE))

    def test_a_good_block_passes(self):
        self.assertEqual(call_templates.check_preset(GOOD_PRESET), GOOD_PRESET)
        minimal = {
            "organization": "Org",
            "process": "Process",
            "category": "programme",
            "summary": "One line.",
        }
        self.assertEqual(call_templates.check_preset(minimal), minimal)
        # A generic pattern needs no organisation; a programme's process
        # takes the programme's.
        generic = {"process": "Process", "category": "generic", "summary": "x"}
        self.assertEqual(call_templates.check_preset(generic), generic)
        of_programme = {
            "programme": "eurohpc-ju-access",
            "process": "Process",
            "category": "programme",
            "summary": "x",
        }
        self.assertEqual(call_templates.check_preset(of_programme), of_programme)

    @data(
        ({"extra": "x"}, "programmes.p"),
        ({"name": ""}, "programmes.p.name"),
        ({"organization": None}, "programmes.p.organization"),
        ({"summary": "x" * 301}, "programmes.p.summary"),
        ({"reference_url": "javascript:alert(1)"}, "programmes.p.reference_url"),
    )
    def test_a_bad_programme_is_refused(self, case):
        change, field = case
        entry = {
            "name": "Programme",
            "organization": "Org",
            "organization_url": "https://org.example.org",
            "reference_url": "https://org.example.org/calls",
            "summary": "One line.",
            **change,
        }
        with self.assertRaises(drf_serializers.ValidationError) as cm:
            call_templates.check_programme("p", entry)
        self.assertIn(field, cm.exception.detail)

    @data(
        ({"extra": "x"}, "preset"),
        ({"organization": "", "category": "programme"}, "preset.organization"),
        ({"organization": "x" * 151}, "preset.organization"),
        ({"process": None}, "preset.process"),
        ({"summary": 5}, "preset.summary"),
        ({"category": "other"}, "preset.category"),
        ({"reference_url": "javascript:alert(1)"}, "preset.reference_url"),
        ({"reference_url": "ftp://example.org/x"}, "preset.reference_url"),
        ({"organization_url": "https://e.org/" + "x" * 500}, "preset.organization_url"),
        ({"tags": "HPC"}, "preset.tags"),
        ({"tags": [""]}, "preset.tags"),
        ({"tags": ["x"] * 11}, "preset.tags"),
        ({"tags": ["x" * 41]}, "preset.tags"),
        ({"programme": "no-such-programme"}, "preset.programme"),
        ({"programme": 5}, "preset.programme"),
        # A generic pattern belongs to no programme.
        ({"programme": "eurohpc-ju-access"}, "preset.category"),
        ({"category": "programme", "organization": None}, "preset.organization"),
    )
    def test_a_bad_block_is_refused(self, case):
        change, field = case
        block = {**GOOD_PRESET, **change}
        with self.assertRaises(drf_serializers.ValidationError) as cm:
            call_templates.check_preset(block)
        self.assertIn(field, cm.exception.detail)

    def test_a_block_that_is_not_a_mapping_is_refused(self):
        with self.assertRaises(drf_serializers.ValidationError):
            call_templates.check_preset(["organization"])

    def create(self, preset):
        self.client.force_authenticate(self.fixture.staff)
        document = {**call_templates.load(TEMPLATE), "preset": preset}
        return self.client.post(
            list_url(), {"name": "Access call", "document": document}, format="json"
        )

    def test_staff_template_with_a_bad_block_is_refused(self):
        response = self.create({**GOOD_PRESET, "reference_url": "file:///etc/passwd"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("preset.reference_url", str(response.data["document"]))

    def test_staff_template_with_its_own_block(self):
        response = self.create(GOOD_PRESET)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["preset"], {**GOOD_PRESET, "programme": None})
        self.assertEqual(
            response.data["origin"],
            {
                "organization": "Example Research Council",
                "process": "Open access call",
                "reference_url": "https://research.example.org/calls",
                "programme": None,
            },
        )

    def test_staff_template_naming_a_programme(self):
        block = {
            "programme": "eurohpc-ju-access",
            "process": "Local access mode",
            "category": "programme",
            "summary": "A local variant.",
        }
        response = self.create(block)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data["preset"]["programme"]["key"], block["programme"]
        )
        self.assertEqual(
            response.data["preset"]["organization"], "EuroHPC Joint Undertaking"
        )
        self.assertEqual(
            response.data["origin"],
            {
                "organization": "EuroHPC Joint Undertaking",
                "process": "Local access mode",
                "reference_url": None,
                "programme": "EuroHPC JU access calls",
            },
        )

    def test_staff_template_naming_an_unknown_programme_is_refused(self):
        response = self.create(
            {
                "programme": "no-such-programme",
                "process": "Process",
                "category": "programme",
                "summary": "x",
            }
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("preset.programme", str(response.data["document"]))

    def test_importing_a_document_with_the_block_ignores_it(self):
        manager = self.fixture.manager
        call, _, warnings = call_transfer.import_call(
            {**call_templates.load(TEMPLATE), "preset": GOOD_PRESET},
            manager,
            self.fixture.staff,
        )
        self.assertEqual(warnings, [])
        document, _ = call_transfer.export_call(call)
        self.assertNotIn("preset", document)

    def test_presets_list_carries_the_block(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(presets_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        (preset,) = [p for p in response.data if p["key"] == TEMPLATE]
        self.assertEqual(preset["preset"], call_templates.preset_info(TEMPLATE))
        self.assertEqual(preset["preset"]["organization"], "EuroHPC Joint Undertaking")
        self.assertEqual(preset["preset"]["category"], "programme")
        programme = preset["preset"]["programme"]
        self.assertEqual(programme["key"], "eurohpc-ju-access")
        self.assertEqual(programme["name"], "EuroHPC JU access calls")
        self.assertEqual(programme["organization_url"], "https://eurohpc-ju.europa.eu")
        self.assertIn("name_taken", preset)

    def load_published(self):
        call_templates.load_presets([TEMPLATE], publish=True)
        return models.CallTemplate.objects.get(name="Extreme Scale Access")

    def test_a_loaded_preset_keeps_the_block_in_its_document(self):
        template = self.load_published()
        self.assertEqual(
            template.document["preset"], call_templates.preset_block(TEMPLATE)
        )

    def test_staff_sees_the_block_of_the_preset(self):
        template = self.load_published()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(detail_url(template))
        self.assertEqual(response.data["preset"], call_templates.preset_info(TEMPLATE))
        self.assertEqual(
            response.data["origin"]["organization"], "EuroHPC Joint Undertaking"
        )

    def test_call_manager_sees_only_the_public_origin(self):
        template = self.load_published()
        self.client.force_authenticate(self.organizer)
        response = self.client.get(detail_url(template))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        info = call_templates.preset_info(TEMPLATE)
        self.assertEqual(
            response.data["origin"],
            {
                "organization": info["organization"],
                "process": info["process"],
                "reference_url": info["reference_url"],
                "programme": "EuroHPC JU access calls",
            },
        )
        self.assertNotIn("preset", response.data)

    def test_template_without_a_block_has_no_origin(self):
        template = make_template()
        self.client.force_authenticate(self.organizer)
        response = self.client.get(detail_url(template))
        self.assertIsNone(response.data["origin"])

    def test_a_malformed_stored_block_is_not_shown(self):
        # Written before blocks were checked.
        template = make_template(
            document={**call_templates.load(TEMPLATE), "preset": {"organization": 1}}
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(detail_url(template))
        self.assertIsNone(response.data["preset"])
        self.assertIsNone(response.data["origin"])
