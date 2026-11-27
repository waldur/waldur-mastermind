import copy
import json
from pathlib import Path
from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.tests.helpers import create_pat
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, offering_templates
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING
from waldur_mastermind.marketplace.tests import factories

LIST_URL = "/api/marketplace-offering-templates/"
# Presets the tests load, which are not shipped: SLURM with DWDI reporting.
TEST_PRESETS = Path(__file__).resolve().parent / "offering_template_presets"


def detail_url(template, action=""):
    url = f"{LIST_URL}{template.uuid.hex}/"
    return f"{url}{action}/" if action else url


def make_document(**overrides):
    document = {
        "schema_version": 1,
        "offering": {"name": "HPC", "description": "", "type": SITE_AGENT_OFFERING},
        "agent_configuration": {
            "order_processing_backend": "slurm",
            "backend_settings": {"qos_default": "normal"},
        },
        "components": [
            {
                "type": "node",
                "name": "Node hours",
                "billing_type": "limit",
                "measured_unit": "node-hours",
            }
        ],
        "plans": [{"name": "Default", "components": []}],
        "parameters": [
            {
                "key": "cluster_name",
                "label": "Cluster name",
                "paths": [
                    "offering.backend_id",
                    "agent_configuration.backend_settings.cluster_name",
                ],
            },
            {
                "key": "default_account",
                "label": "Default account",
                "default": "root",
                "paths": ["agent_configuration.backend_settings.default_account"],
            },
            {
                "key": "customer_prefix",
                "label": "Customer prefix",
                "paths": ["agent_configuration.backend_settings.customer_prefix"],
            },
            {
                "key": "project_prefix",
                "label": "Project prefix",
                "paths": ["agent_configuration.backend_settings.project_prefix"],
            },
            {
                "key": "allocation_prefix",
                "label": "Allocation prefix",
                "paths": ["agent_configuration.backend_settings.allocation_prefix"],
            },
        ],
    }
    document.update(overrides)
    return document


def make_template(**kwargs):
    values = {
        "name": "SLURM",
        "offering_type": SITE_AGENT_OFFERING,
        "document": offering_templates.check_document(
            make_document(), SITE_AGENT_OFFERING
        ),
        "schema_version": 1,
    }
    values.update(kwargs)
    return models.OfferingTemplate.objects.create(**values)


class BaseOfferingTemplateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.owner = self.fixture.owner
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        self.stranger = structure_factories.UserFactory()


class OfferingTemplateDocumentTest(BaseOfferingTemplateTest):
    def create(self, document, user=None):
        self.client.force_authenticate(user or self.staff)
        return self.client.post(
            LIST_URL,
            {
                "name": "SLURM",
                "offering_type": SITE_AGENT_OFFERING,
                "document": document,
            },
            format="json",
        )

    def test_staff_creates_template_with_first_version(self):
        response = self.create(make_document())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        template = models.OfferingTemplate.objects.get()
        self.assertEqual(template.version, 1)
        self.assertEqual(template.created_by, self.staff)
        self.assertEqual(list(template.versions.values_list("number", flat=True)), [1])
        # Defaults are written into each parameter.
        parameter = template.document["parameters"][0]
        self.assertEqual(parameter["type"], "string")
        self.assertTrue(parameter["required"])

    def test_document_may_be_yaml(self):
        response = self.create(json.dumps(make_document()))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_secret_options_section_is_refused(self):
        response = self.create(make_document(secret_options={"api_token": "x"}))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("secret_options", response.data["document"])
        self.assertFalse(models.OfferingTemplate.objects.exists())

    def test_excluded_and_unknown_sections_are_refused(self):
        for section in ("organization_groups", "customer", "resources", "users"):
            response = self.create(make_document(**{section: []}))
            self.assertEqual(response.status_code, 400, section)
            self.assertIn(section, response.data["document"])
        response = self.create(make_document(whatever={}))
        self.assertEqual(response.status_code, 400)

    def test_secret_in_agent_configuration_is_refused(self):
        document = make_document()
        document["agent_configuration"]["backend_settings"][
            "cscs_dwdi_client_secret"
        ] = "s3cret"
        response = self.create(document)
        self.assertEqual(response.status_code, 400)
        self.assertIn("agent_configuration", response.data["document"])

    def test_secret_in_plugin_options_is_refused(self):
        response = self.create(make_document(plugin_options={"api_token": "x"}))
        self.assertEqual(response.status_code, 400)

    def test_secret_anywhere_in_the_document_is_refused_with_its_path(self):
        offering = make_document()["offering"]
        cases = (
            (
                {"offering": {**offering, "options": {"api_token": "x"}}},
                "offering.options.api_token",
            ),
            (
                {
                    "offering": {
                        **offering,
                        "attributes": {"nested": [{"password": "x"}]},
                    }
                },
                "offering.attributes.nested[0].password",
            ),
            ({"resource_options": {"secret": "x"}}, "resource_options.secret"),
            (
                {
                    "components": [
                        {
                            "type": "node",
                            "name": "Node hours",
                            "billing_type": "limit",
                            "measured_unit": "node-hours",
                            "access_key": "x",
                        }
                    ]
                },
                "components[0].access_key",
            ),
            (
                {"endpoints": [{"name": "API", "url": "https://x", "token": "x"}]},
                "endpoints[0].token",
            ),
        )
        for overrides, path in cases:
            with self.subTest(path=path):
                response = self.create(make_document(**overrides))
                self.assertEqual(response.status_code, 400)
                self.assertIn(path, response.data["document"])
        self.assertFalse(models.OfferingTemplate.objects.exists())

    def test_parameter_field_named_key_and_component_type_are_no_secrets(self):
        document = make_document()
        document["agent_configuration"]["backend_components"] = {
            "license_key": {"unit_factor_reporting": 1}
        }
        response = self.create(document)
        self.assertEqual(response.status_code, 201, response.data)

    def test_yaml_aliases_are_refused_and_nothing_is_stored(self):
        document = (
            "schema_version: 1\n"
            "offering:\n"
            "  name: HPC\n"
            f"  type: {SITE_AGENT_OFFERING}\n"
            "  attributes:\n"
            "    a: &a [x, x, x, x, x, x, x, x, x, x]\n"
            "    b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
            "    c: [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
        )
        response = self.create(document)
        self.assertEqual(response.status_code, 400)
        self.assertIn("anchors and aliases", str(response.data["document"]))
        self.assertFalse(models.OfferingTemplate.objects.exists())

    def test_document_over_the_size_limit_is_refused(self):
        big = "x" * offering_templates.DOCUMENT_MAX_BYTES
        offering = {**make_document()["offering"], "description": big}
        for document in (
            make_document(offering=offering),
            json.dumps(make_document(offering=offering)),
        ):
            with self.subTest(yaml=isinstance(document, str)):
                response = self.create(document)
                self.assertEqual(response.status_code, 400)
                self.assertIn("larger than 1 MB", str(response.data["document"]))
        self.assertFalse(models.OfferingTemplate.objects.exists())

    def test_required_setting_not_filled_by_a_parameter_is_refused(self):
        document = make_document()
        document["parameters"] = [
            parameter
            for parameter in document["parameters"]
            if parameter["key"] != "project_prefix"
        ]
        response = self.create(document)
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "project_prefix",
            response.data["document"]["agent_configuration"]["backend_settings"],
        )

    def test_parameter_path_holding_a_value_is_refused(self):
        document = make_document()
        document["agent_configuration"]["backend_settings"]["default_account"] = "root"
        response = self.create(document)
        self.assertEqual(response.status_code, 400)
        self.assertIn("parameters.1.paths", response.data["document"])

    def test_parameter_may_not_ask_for_a_secret(self):
        document = make_document()
        document["parameters"].append(
            {
                "key": "secret",
                "label": "Secret",
                "paths": [
                    "agent_configuration.backend_settings.cscs_dwdi_client_secret"
                ],
            }
        )
        response = self.create(document)
        self.assertEqual(response.status_code, 400)

    def test_parameter_paths_must_lead_into_the_document(self):
        for path in ("customer.name", "endpoints.0.url", "offering"):
            document = make_document()
            document["parameters"].append(
                {"key": "extra", "label": "Extra", "paths": [path]}
            )
            response = self.create(document)
            self.assertEqual(response.status_code, 400, path)

    def test_two_parameters_may_not_fill_one_path(self):
        document = make_document()
        document["parameters"].append(
            {"key": "extra", "label": "Extra", "paths": ["offering.backend_id"]}
        )
        response = self.create(document)
        self.assertEqual(response.status_code, 400)

    def test_default_must_match_type(self):
        document = make_document()
        document["parameters"][1]["type"] = "integer"
        response = self.create(document)
        self.assertEqual(response.status_code, 400)

    def test_offering_block_must_be_of_the_template_type(self):
        document = make_document()
        document["offering"]["type"] = "Marketplace.Basic"
        response = self.create(document)
        self.assertEqual(response.status_code, 400)

    def test_other_offering_types_are_refused(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            LIST_URL,
            {
                "name": "Basic",
                "offering_type": "Marketplace.Basic",
                "document": make_document(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("offering_type", response.data)

    def test_unsupported_schema_version_is_refused(self):
        response = self.create(make_document(schema_version=99))
        self.assertEqual(response.status_code, 400)

    def test_duplicate_name_is_refused(self):
        make_template()
        response = self.create(make_document())
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.data)


class OfferingTemplateVersionTest(BaseOfferingTemplateTest):
    def setUp(self):
        super().setUp()
        self.template = make_template(created_by=self.staff)
        self.client.force_authenticate(self.staff)

    def test_name_description_and_publication_keep_the_version(self):
        response = self.client.patch(
            detail_url(self.template),
            {"name": "Renamed", "description": "New", "is_published": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.template.refresh_from_db()
        self.assertEqual(self.template.version, 1)
        self.assertEqual(self.template.versions.count(), 1)

    def test_same_document_in_another_key_order_keeps_the_version(self):
        reordered = dict(reversed(list(self.template.document.items())))
        response = self.client.patch(
            detail_url(self.template), {"document": reordered}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.template.refresh_from_db()
        self.assertEqual(self.template.version, 1)

    def test_document_change_bumps_version_and_stores_snapshot(self):
        editor = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(editor)
        document = copy.deepcopy(self.template.document)
        document["offering"]["description"] = "Changed"
        response = self.client.patch(
            detail_url(self.template), {"document": document}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["version"], 2)

        response = self.client.get(detail_url(self.template, "versions"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([v["number"] for v in response.data], [1, 2])
        self.assertEqual(
            response.data[1]["document"]["offering"]["description"], "Changed"
        )
        self.assertEqual(str(response.data[1]["created_by"]), editor.uuid.hex)
        self.assertEqual(str(response.data[0]["created_by"]), self.staff.uuid.hex)

    def test_version_is_read_only(self):
        response = self.client.patch(
            detail_url(self.template), {"version": 7}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.template.refresh_from_db()
        self.assertEqual(self.template.version, 1)


@ddt
class OfferingTemplatePermissionTest(BaseOfferingTemplateTest):
    def setUp(self):
        super().setUp()
        self.published = make_template(name="Published", is_published=True)
        self.draft = make_template(name="Draft")

    @data("owner", "stranger")
    def test_only_staff_write(self, user):
        self.client.force_authenticate(getattr(self, user))
        response = self.client.post(
            LIST_URL,
            {
                "name": "New",
                "offering_type": SITE_AGENT_OFFERING,
                "document": make_document(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.patch(
            detail_url(self.published), {"is_published": False}, format="json"
        )
        self.assertIn(response.status_code, (403, 404))
        response = self.client.delete(detail_url(self.published))
        self.assertIn(response.status_code, (403, 404))
        response = self.client.post(
            f"{LIST_URL}load_preset/", {"presets": ["slurm"]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.get(f"{LIST_URL}presets/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.OfferingTemplate.objects.filter(pk=self.published.pk).exists()
        )

    def test_staff_sees_every_template_in_full(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(LIST_URL)
        self.assertEqual({t["name"] for t in response.data}, {"Published", "Draft"})
        template = next(t for t in response.data if t["name"] == "Published")
        self.assertIn("agent_configuration", template["document"])
        self.assertIn("created_by", template)

    def test_offering_creator_sees_public_part_of_published_templates(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([t["name"] for t in response.data], ["Published"])
        template = response.data[0]
        self.assertNotIn("created_by", template)
        self.assertNotIn("agent_configuration", template["document"])
        self.assertEqual(
            set(template["document"]),
            {"schema_version", "offering", "components", "plans", "parameters"},
        )
        self.assertEqual(
            [p["key"] for p in template["parameters"]],
            [p["key"] for p in self.published.document["parameters"]],
        )
        response = self.client.get(detail_url(self.draft))
        self.assertEqual(response.status_code, 404)

    def test_offering_creator_sees_the_role_backends_only(self):
        # The roles an offering created from the template starts with, without
        # the rest of its agent configuration.
        self.client.force_authenticate(self.owner)
        template = self.client.get(detail_url(self.published)).data
        self.assertEqual(template["agent_roles"], {"order_processing_backend": "slurm"})
        self.assertNotIn("agent_configuration", template["document"])
        response = self.client.get(detail_url(self.published, "versions"))
        self.assertEqual(response.status_code, 403)

    def test_user_who_may_not_create_offerings_sees_none(self):
        self.client.force_authenticate(self.stranger)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def list_with_token(self, user, scopes):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        _, token = create_pat(user, scopes=scopes)
        return self.client.get(LIST_URL, HTTP_AUTHORIZATION=f"Bearer {token}")

    @override_config(PAT_ENABLED=True)
    def test_token_without_create_offering_sees_none(self):
        response = self.list_with_token(
            self.owner, [PermissionEnum.LIST_PROJECTS.value]
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    @override_config(PAT_ENABLED=True)
    def test_token_with_create_offering_sees_published_templates(self):
        response = self.list_with_token(
            self.owner, [PermissionEnum.CREATE_OFFERING.value]
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([t["name"] for t in response.data], ["Published"])

    @override_config(PAT_ENABLED=True)
    def test_staff_token_without_staff_or_create_offering_scope_sees_none(self):
        response = self.list_with_token(
            self.staff, [PermissionEnum.LIST_PROJECTS.value]
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])


class OfferingTemplatePresetTest(BaseOfferingTemplateTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.staff)

    def test_shipped_presets(self):
        self.assertEqual(offering_templates.names(), ["litellm", "slurm"])

    def test_every_preset_is_a_valid_document(self):
        for directory in (offering_templates.PRESETS_DIRECTORY, TEST_PRESETS):
            with mock.patch.object(offering_templates, "PRESETS_DIRECTORY", directory):
                for key in offering_templates.names():
                    preset = offering_templates.load(key)
                    offering_templates.check_document(
                        preset["document"], preset["offering_type"]
                    )

    def test_list_presets(self):
        response = self.client.get(f"{LIST_URL}presets/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["key"] for p in response.data], ["litellm", "slurm"])
        preset = next(p for p in response.data if p["key"] == "slurm")
        self.assertFalse(preset["name_taken"])
        self.assertIn("cluster_name", [p["key"] for p in preset["parameters"]])
        preset = next(p for p in response.data if p["key"] == "litellm")
        self.assertEqual(
            [p["key"] for p in preset["parameters"]],
            ["api_url", "default_monthly_spending_limit"],
        )

    def test_load_presets(self):
        response = self.client.post(
            f"{LIST_URL}load_preset/",
            {"presets": ["slurm", "litellm"], "publish": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["created"], 2)
        for template in models.OfferingTemplate.objects.all():
            self.assertTrue(template.is_published)
            self.assertEqual(template.created_by, self.staff)
            self.assertEqual(template.versions.count(), 1)

    def test_load_preset_kept_for_tests(self):
        with mock.patch.object(offering_templates, "PRESETS_DIRECTORY", TEST_PRESETS):
            response = self.client.post(
                f"{LIST_URL}load_preset/", {"presets": ["slurm_dwdi"]}, format="json"
            )
        self.assertEqual(response.status_code, 200, response.data)
        document = models.OfferingTemplate.objects.get().document
        self.assertEqual(
            document["agent_configuration"]["reporting_backend"], "cscs-dwdi-compute"
        )

    def test_loading_a_preset_whose_name_is_taken_creates_nothing(self):
        name = offering_templates.load("slurm")["name"]
        make_template(name=name)
        response = self.client.post(
            f"{LIST_URL}load_preset/",
            {"presets": ["litellm", "slurm"]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        # Nor the preset listed before the refused one.
        self.assertEqual(models.OfferingTemplate.objects.count(), 1)
        self.assertEqual(models.OfferingTemplateVersion.objects.count(), 1)

    def test_unknown_preset_is_refused(self):
        response = self.client.post(
            f"{LIST_URL}load_preset/", {"presets": ["slurm_dwdi"]}, format="json"
        )
        self.assertEqual(response.status_code, 400)


class SaveOfferingAsTemplateTest(BaseOfferingTemplateTest):
    def setUp(self):
        super().setUp()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=SITE_AGENT_OFFERING,
            backend_id="alpha",
            access_url="https://alpha.example.com",
            plugin_options={"api_token": "plugin-secret-value"},
            secret_options={"api_url": "https://x", "token": "secret-options-value"},
            agent_configuration={
                "order_processing_backend": "slurm",
                "reporting_backend": "cscs-dwdi-compute",
                "backend_settings": {
                    "default_account": "root",
                    "customer_prefix": "alpha_c_",
                    "project_prefix": "alpha_p_",
                    "allocation_prefix": "alpha_a_",
                    "cluster_name": "alpha",
                    "qos_default": "normal",
                    "cscs_dwdi_api_url": "https://dwdi.alpha.example.com",
                    "cscs_dwdi_client_id": "alpha-client",
                    "cscs_dwdi_oidc_token_url": "https://id.alpha.example.com/token",
                    # Written around the serializer, which refuses secrets.
                    "cscs_dwdi_client_secret": "dwdi-secret-value",
                },
                "backend_components": {
                    "node": {"unit_factor_reporting": 1, "license_key": "k-value"}
                },
            },
        )
        factories.OfferingComponentFactory(
            offering=self.offering, type="node", name="Node hours"
        )
        models.OfferingAccessEndpoint.objects.create(
            offering=self.offering, name="Login", url="ssh://login.alpha.example.com"
        )
        self.url = (
            f"/api/marketplace-provider-offerings/{self.offering.uuid.hex}/"
            "save_as_template/"
        )

    def test_staff_saves_offering_as_unpublished_template(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {"name": "Alpha"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        template = models.OfferingTemplate.objects.get()
        self.assertEqual(template.name, "Alpha")
        self.assertFalse(template.is_published)
        self.assertEqual(template.offering_type, SITE_AGENT_OFFERING)
        self.assertEqual(template.created_by, self.staff)
        self.assertEqual(template.versions.count(), 1)

    def test_template_never_holds_secrets(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        stored = json.dumps(models.OfferingTemplate.objects.get().document)
        for secret in (
            "plugin-secret-value",
            "secret-options-value",
            "dwdi-secret-value",
            "k-value",
        ):
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret, json.dumps(response.data, default=str))
        self.assertNotIn("secret_options", stored)

    def test_secrets_in_other_sections_are_dropped(self):
        self.offering.options = {
            "order": ["ssh_public_key", "size"],
            "options": {
                # A field the customer fills in: its name is not a secret.
                "ssh_public_key": {"type": "string", "label": "SSH key"},
                "size": {
                    "type": "integer",
                    "label": "Size",
                    "api_token": "option-secret-value",
                },
            },
        }
        self.offering.resource_options = {"admin_password": "resource-secret-value"}
        self.offering.attributes = {"nested": {"token": "attribute-secret-value"}}
        self.offering.save()
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        document = models.OfferingTemplate.objects.get().document
        stored = json.dumps(document)
        for secret in (
            "resource-secret-value",
            "attribute-secret-value",
            "option-secret-value",
        ):
            self.assertNotIn(secret, stored)
        form = document["offering"]["options"]
        self.assertEqual(form["order"], ["ssh_public_key", "size"])
        self.assertEqual(set(form["options"]), {"ssh_public_key", "size"})
        self.assertEqual(form["options"]["size"], {"type": "integer", "label": "Size"})
        self.assertEqual(offering_templates.find_secrets(document), [])

    def test_order_form_field_names_are_not_secrets(self):
        document = make_document(
            offering={
                "name": "HPC",
                "type": SITE_AGENT_OFFERING,
                "options": {
                    "order": ["ssh_public_key"],
                    "options": {
                        "ssh_public_key": {"type": "string", "label": "SSH key"}
                    },
                },
            },
            resource_options={
                "options": {"api_key_name": {"type": "string", "label": "Key name"}}
            },
        )
        self.assertEqual(offering_templates.find_secrets(document), [])
        document["offering"]["options"]["options"]["ssh_public_key"]["password"] = "x"
        self.assertEqual(
            offering_templates.find_secrets(document),
            ["offering.options.options.ssh_public_key.password"],
        )

    def test_site_specific_values_become_parameters(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        document = models.OfferingTemplate.objects.get().document
        stored = json.dumps(
            {key: value for key, value in document.items() if key != "parameters"}
        )
        for value in (
            "alpha_c_",
            "alpha_p_",
            "alpha_a_",
            "dwdi.alpha.example.com",
            "id.alpha.example.com",
            "alpha-client",
            "alpha.example.com",
            "login.alpha.example.com",
        ):
            self.assertNotIn(value, stored)
        parameters = {p["key"]: p for p in document["parameters"]}
        self.assertEqual(
            parameters["cluster_name"]["paths"],
            [
                "offering.backend_id",
                "agent_configuration.backend_settings.cluster_name",
            ],
        )
        # What identifies the site has no default...
        for key in (
            "cscs_dwdi_api_url",
            "cscs_dwdi_client_id",
            "cscs_dwdi_oidc_token_url",
            "access_url",
            "endpoint_1_url",
        ):
            self.assertIn(key, parameters)
            self.assertTrue(parameters[key]["required"])
            self.assertIsNone(parameters[key]["default"])
        # ...while the site's conventions offer the offering's values.
        self.assertEqual(
            {
                key: parameters[key]["default"]
                for key in (
                    "default_account",
                    "customer_prefix",
                    "project_prefix",
                    "allocation_prefix",
                )
            },
            {
                "default_account": "root",
                "customer_prefix": "alpha_c_",
                "project_prefix": "alpha_p_",
                "allocation_prefix": "alpha_a_",
            },
        )
        # What is not site-specific is kept.
        self.assertEqual(
            document["agent_configuration"]["backend_settings"]["qos_default"],
            "normal",
        )
        self.assertEqual(document["endpoints"][0]["name"], "Login")
        self.assertNotIn("state", document["offering"])

    def test_name_defaults_to_offering_name(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.data["name"], self.offering.name)

    def test_owner_may_not_save_as_template(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.OfferingTemplate.objects.exists())

    def test_other_offering_types_are_refused(self):
        self.offering.type = "Marketplace.Basic"
        self.offering.save()
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_litellm_proxy_url_becomes_a_parameter(self):
        self.offering.agent_configuration = {
            "order_processing_backend": "litellm",
            "reporting_backend": "litellm-usage",
            "backend_settings": {
                "api_url": "https://litellm.alpha.example.com",
                "api_token": "sk-master-value",
                "verify_ssl": True,
            },
        }
        self.offering.save()
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        document = models.OfferingTemplate.objects.get().document
        stored = json.dumps(document)
        self.assertNotIn("litellm.alpha.example.com", stored)
        self.assertNotIn("sk-master-value", stored)
        parameters = {p["key"]: p for p in document["parameters"]}
        self.assertEqual(
            parameters["api_url"]["paths"],
            ["agent_configuration.backend_settings.api_url"],
        )
        self.assertTrue(
            document["agent_configuration"]["backend_settings"]["verify_ssl"]
        )


class SaveAsTemplateUsernamePolicyTest(test.APITestCase):
    def test_username_policy_becomes_a_parameter_with_its_value_as_default(self):
        offering = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            backend_id="alps",
            plugin_options={"username_generation_policy": "full_name"},
        )
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.post(
            f"/api/marketplace-provider-offerings/{offering.uuid.hex}/save_as_template/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        document = models.OfferingTemplate.objects.get().document
        self.assertNotIn("username_generation_policy", document["plugin_options"])
        parameter = {p["key"]: p for p in document["parameters"]}[
            "username_generation_policy"
        ]
        self.assertEqual(parameter["default"], "full_name")
        self.assertIn("anonymized", parameter["choices"])
        self.assertEqual(
            parameter["paths"], ["plugin_options.username_generation_policy"]
        )
