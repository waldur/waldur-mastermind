import copy
from decimal import Decimal

from ddt import data, ddt
from rest_framework import serializers, status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, offering_templates
from waldur_mastermind.marketplace.enums import BASIC_OFFERING, SITE_AGENT_OFFERING
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_offering_templates import (
    LIST_URL,
    detail_url,
    make_document,
)

PROVIDER_OFFERINGS_URL = "/api/marketplace-provider-offerings/"

PARAMETERS = {
    "cluster_name": "alps",
    "customer_prefix": "c_",
    "project_prefix": "p_",
    "allocation_prefix": "a_",
}


def priced_document(**overrides):
    document = make_document(
        plans=[
            {
                "name": "Default",
                "unit": "month",
                "components": [{"component_type": "node", "amount": 0, "price": "1.5"}],
            }
        ],
    )
    document.update(overrides)
    return document


def make_template(document=None, **kwargs):
    values = {
        "name": "SLURM",
        "offering_type": SITE_AGENT_OFFERING,
        "document": offering_templates.check_document(
            document or priced_document(), SITE_AGENT_OFFERING
        ),
        "schema_version": 1,
        "is_published": True,
    }
    values.update(kwargs)
    return models.OfferingTemplate.objects.create(**values)


class BaseCreateOfferingTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.owner = self.fixture.owner
        factories.ServiceProviderFactory(customer=self.customer)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        CustomerRole.OWNER.add_permission(
            PermissionEnum.UPDATE_OFFERING_AGENT_CONFIGURATION
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.category = factories.CategoryFactory()
        self.template = make_template()

    def create_offering(self, template=None, user=None, **overrides):
        self.client.force_authenticate(user or self.owner)
        payload = {
            "customer": self.customer.uuid.hex,
            "category": self.category.uuid.hex,
            "name": "Alps",
            "parameters": PARAMETERS,
        }
        payload.update(overrides)
        return self.client.post(
            detail_url(template or self.template, "create_offering"),
            payload,
            format="json",
        )


class CreateOfferingTest(BaseCreateOfferingTest):
    def test_creates_draft_offering_as_resolved(self):
        response = self.create_offering()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        self.assertEqual(offering.state, models.Offering.States.DRAFT)
        self.assertEqual(offering.name, "Alps")
        self.assertEqual(offering.type, SITE_AGENT_OFFERING)
        self.assertEqual(offering.customer, self.customer)
        self.assertEqual(offering.category, self.category)
        self.assertEqual(offering.backend_id, "alps")
        self.assertEqual(
            offering.agent_configuration["backend_settings"],
            {
                "qos_default": "normal",
                "cluster_name": "alps",
                "default_account": "root",
                "customer_prefix": "c_",
                "project_prefix": "p_",
                "allocation_prefix": "a_",
            },
        )
        component = offering.components.get()
        self.assertEqual(component.type, "node")
        plan_component = offering.plans.get().components.get()
        self.assertEqual(plan_component.price, Decimal("1.5"))

    def test_offering_records_template_version_and_values(self):
        response = self.create_offering()
        offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        self.assertEqual(offering.template, self.template)
        self.assertEqual(offering.template_version, 1)
        self.assertEqual(
            offering.template_parameters, {**PARAMETERS, "default_account": "root"}
        )
        self.client.force_authenticate(self.owner)
        details = self.client.get(f"{PROVIDER_OFFERINGS_URL}{offering.uuid.hex}/").data
        self.assertEqual(details["template_uuid"], self.template.uuid.hex)
        self.assertEqual(details["template_name"], "SLURM")
        self.assertEqual(details["template_version"], 1)

    def test_response_reports_values_and_filled_document(self):
        response = self.create_offering()
        self.assertFalse(response.data["dry_run"])
        self.assertEqual(response.data["template_version"], 1)
        self.assertEqual(response.data["parameters"]["default_account"], "root")
        document = response.data["document"]
        self.assertNotIn("parameters", document)
        self.assertEqual(document["offering"]["backend_id"], "alps")

    def test_missing_required_parameter_is_refused_per_parameter(self):
        response = self.create_offering(parameters={"cluster_name": "alps"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            set(response.data["parameters"]),
            {"customer_prefix", "project_prefix", "allocation_prefix"},
        )
        self.assertFalse(models.Offering.objects.exists())

    def test_value_of_wrong_type_is_refused(self):
        response = self.create_offering(parameters={**PARAMETERS, "cluster_name": 7})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["parameters"]["cluster_name"], ["Expected a string."]
        )

    def test_undeclared_parameter_is_refused(self):
        response = self.create_offering(parameters={**PARAMETERS, "qos": "high"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("qos", response.data["parameters"])

    def test_name_taken_in_the_organization_is_refused(self):
        factories.OfferingFactory(customer=self.customer, name="Alps")
        response = self.create_offering()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data)

    def test_dry_run_creates_nothing(self):
        counts = (
            models.Offering.objects.count(),
            models.OfferingComponent.objects.count(),
            models.Plan.objects.count(),
            models.PlanComponent.objects.count(),
        )
        response = self.create_offering(dry_run=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["dry_run"])
        self.assertIsNone(response.data["offering_uuid"])
        self.assertEqual(response.data["document"]["offering"]["backend_id"], "alps")
        self.assertEqual(
            counts,
            (
                models.Offering.objects.count(),
                models.OfferingComponent.objects.count(),
                models.Plan.objects.count(),
                models.PlanComponent.objects.count(),
            ),
        )

    def test_dry_run_reports_what_the_import_refuses(self):
        document = priced_document()
        document["components"][0]["billing_type"] = "unknown"
        template = make_template(document, name="Broken")
        response = self.create_offering(template, dry_run=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.Offering.objects.exists())

    def test_editing_template_changes_no_offering_created_from_it(self):
        response = self.create_offering()
        offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        document = copy.deepcopy(self.template.document)
        document["components"][0]["name"] = "Nodes"
        document["agent_configuration"]["backend_settings"]["qos_default"] = "high"
        self.template.document = document
        self.template.save()
        self.template.refresh_from_db()
        self.assertEqual(self.template.version, 2)
        offering.refresh_from_db()
        self.assertEqual(offering.components.get().name, "Node hours")
        self.assertEqual(
            offering.agent_configuration["backend_settings"]["qos_default"], "normal"
        )
        self.assertEqual(offering.template_version, 1)

    def test_optional_parameter_without_value_leaves_its_path_out(self):
        document = priced_document()
        document["parameters"].append(
            {
                "key": "qos_paused",
                "label": "Paused QoS",
                "required": False,
                "paths": ["agent_configuration.backend_settings.qos_paused"],
            }
        )
        template = make_template(document, name="Optional")
        response = self.create_offering(template)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        self.assertNotIn("qos_paused", offering.agent_configuration["backend_settings"])


class CreateOfferingFindingsTest(BaseCreateOfferingTest):
    """The site-agent consistency rules (#815) check the filled-in document."""

    def template_with_component(self, **component):
        document = priced_document()
        document["components"].append(
            {"name": "Extra", "measured_unit": "units", **component}
        )
        document["plans"][0]["components"].append(
            {"component_type": component["type"], "amount": 0, "price": "1"}
        )
        return make_template(document, name="With extra component")

    def test_consistent_offering_has_no_findings(self):
        response = self.create_offering()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["findings"], [])

    def test_error_refuses_the_create(self):
        # SLURM drops a limit on what is not a TRES: storage is neither
        # limited nor billed.
        template = self.template_with_component(type="storage", billing_type="limit")
        response = self.create_offering(template)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        [finding] = response.data["findings"]
        self.assertEqual(finding["rule"], "slurm_component_tres")
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(finding["field"], "components.1.type")
        self.assertFalse(models.Offering.objects.exists())

    def test_dry_run_reports_the_error_and_creates_nothing(self):
        template = self.template_with_component(type="storage", billing_type="limit")
        response = self.create_offering(template, dry_run=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [(f["rule"], f["severity"]) for f in response.data["findings"]],
            [("slurm_component_tres", "error")],
        )
        self.assertFalse(models.Offering.objects.exists())

    def test_warning_does_not_refuse_the_create(self):
        # A fixed fee for a TRES gets no SLURM limit, which can be intended.
        template = self.template_with_component(type="cpu", billing_type="fixed")
        response = self.create_offering(template)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            [(f["rule"], f["severity"]) for f in response.data["findings"]],
            [("slurm_component_billing_type", "warning")],
        )
        self.assertTrue(
            models.Offering.objects.filter(uuid=response.data["offering_uuid"]).exists()
        )


class CreateOfferingAgentRolesTest(BaseCreateOfferingTest):
    """agent_roles replaces the template's role backends before the checks."""

    def test_role_backend_replaces_the_templates(self):
        response = self.create_offering(
            agent_roles={"membership_sync_backend": "slurm"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        self.assertEqual(
            offering.agent_configuration["order_processing_backend"], "slurm"
        )
        self.assertEqual(
            offering.agent_configuration["membership_sync_backend"], "slurm"
        )

    def test_roles_left_out_keep_the_templates(self):
        response = self.create_offering(agent_roles={})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        self.assertEqual(
            offering.agent_configuration["order_processing_backend"], "slurm"
        )
        self.assertNotIn("membership_sync_backend", offering.agent_configuration)

    def test_blank_role_names_no_backend(self):
        response = self.create_offering(
            agent_roles={"order_processing_backend": ""}, dry_run=True
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn(
            "order_processing_backend", response.data["document"]["agent_configuration"]
        )

    def test_dry_run_checks_the_settings_of_the_chosen_backend(self):
        # DWDI needs settings the SLURM template does not carry: the dry run
        # names them, under the agent configuration.
        response = self.create_offering(
            agent_roles={"reporting_backend": "cscs-dwdi-compute"}, dry_run=True
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "cscs_dwdi_api_url",
            response.data["agent_configuration"]["backend_settings"],
        )

    def test_backend_that_cannot_fill_the_role_is_refused(self):
        response = self.create_offering(
            agent_roles={"username_management_backend": "slurm"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username_management_backend", str(response.data))
        self.assertFalse(models.Offering.objects.exists())

    def test_custom_backend_is_accepted_once_declared(self):
        refused = self.create_offering(
            agent_roles={"reporting_backend": "site-usage"}, dry_run=True
        )
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.create_offering(
            agent_roles={
                "reporting_backend": "site-usage",
                "custom_backends": ["site-usage"],
            }
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        self.assertEqual(
            offering.agent_configuration["reporting_backend"], "site-usage"
        )
        self.assertEqual(
            offering.agent_configuration["custom_backends"], ["site-usage"]
        )

    def test_unknown_role_key_is_refused(self):
        response = self.create_offering(agent_roles={"backend_settings": {}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("agent_roles", response.data)


AZURE_ROLES = {
    "order_processing_backend": "azure",
    "membership_sync_backend": "azure",
    "reporting_backend": "azure",
}
AZURE_SETTINGS = {
    "subscription_id": "sub-1",
    "tenant_id": "tenant-1",
    "client_id": "client-1",
}


class CreateOfferingBackendSettingsTest(BaseCreateOfferingTest):
    """backend_settings is merged over the template's after the roles."""

    def test_settings_the_chosen_backends_need_are_taken(self):
        dry_run = self.create_offering(
            agent_roles=AZURE_ROLES, backend_settings=AZURE_SETTINGS, dry_run=True
        )
        self.assertEqual(dry_run.status_code, status.HTTP_200_OK, dry_run.data)
        self.assertEqual(
            dry_run.data["document"]["agent_configuration"]["backend_settings"][
                "tenant_id"
            ],
            "tenant-1",
        )
        self.assertFalse(models.Offering.objects.exists())

        response = self.create_offering(
            agent_roles=AZURE_ROLES, backend_settings=AZURE_SETTINGS
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        settings = offering.agent_configuration["backend_settings"]
        for name, value in AZURE_SETTINGS.items():
            self.assertEqual(settings[name], value)
        # The template's own settings stay.
        self.assertEqual(settings["qos_default"], "normal")

    def test_without_them_the_required_settings_are_named(self):
        response = self.create_offering(agent_roles=AZURE_ROLES, dry_run=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data["agent_configuration"]["backend_settings"]
        for name in AZURE_SETTINGS:
            self.assertIn(name, errors)

    def test_secret_is_refused_without_its_value(self):
        response = self.create_offering(
            agent_roles=AZURE_ROLES,
            backend_settings={**AZURE_SETTINGS, "client_secret": "hunter2"},
            dry_run=True,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("client_secret", response.data["backend_settings"])
        self.assertNotIn("hunter2", str(response.data))

    def test_secret_nested_in_a_block_is_refused(self):
        response = self.create_offering(
            backend_settings={"extra": {"password": "hunter2"}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("extra.password", response.data["backend_settings"])
        self.assertNotIn("hunter2", str(response.data))
        self.assertFalse(models.Offering.objects.exists())

    def test_setting_overrides_the_templates(self):
        response = self.create_offering(
            backend_settings={"qos_default": "high", "customer_prefix": "org_"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        settings = offering.agent_configuration["backend_settings"]
        self.assertEqual(settings["qos_default"], "high")
        # Over the filled-in parameter too, which is what the template sets.
        self.assertEqual(settings["customer_prefix"], "org_")
        self.assertEqual(settings["default_account"], "root")

    def test_nested_mapping_is_merged(self):
        configuration = offering_templates.with_backend_settings(
            {"backend_settings": {"block": {"a": 1, "b": 2}, "c": 3}},
            {"block": {"b": 20}},
        )
        self.assertEqual(
            configuration["backend_settings"], {"block": {"a": 1, "b": 20}, "c": 3}
        )

    def basic_template(self):
        return models.OfferingTemplate.objects.create(
            name="Basic",
            offering_type=BASIC_OFFERING,
            document={"schema_version": 1, "offering": {"name": "Basic"}},
            schema_version=1,
            is_published=True,
        )

    def test_agent_roles_of_other_offering_types_are_refused(self):
        response = self.create_offering(
            self.basic_template(),
            parameters={},
            agent_roles={"order_processing_backend": "slurm"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("agent_roles", response.data)
        self.assertFalse(models.Offering.objects.exists())

    def test_empty_agent_roles_of_other_offering_types_are_ignored(self):
        response = self.create_offering(
            self.basic_template(),
            parameters={},
            agent_roles={},
            backend_settings={},
            dry_run=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_other_offering_types_are_refused(self):
        template = models.OfferingTemplate.objects.create(
            name="Basic",
            offering_type=BASIC_OFFERING,
            document={"schema_version": 1, "offering": {"name": "Basic"}},
            schema_version=1,
            is_published=True,
        )
        response = self.create_offering(
            template, parameters={}, backend_settings={"qos_default": "high"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("backend_settings", response.data)
        self.assertFalse(models.Offering.objects.exists())


class CreateOfferingPermissionTest(BaseCreateOfferingTest):
    def test_unpublished_template_is_usable_by_staff_only(self):
        template = make_template(name="Draft", is_published=False)
        self.assertEqual(
            self.create_offering(template).status_code, status.HTTP_404_NOT_FOUND
        )
        response = self.create_offering(template, user=self.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_owner_cannot_create_for_another_organization(self):
        other = structure_factories.CustomerFactory()
        response = self.create_offering(customer=other.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.Offering.objects.exists())

    def test_user_who_may_create_no_offering_sees_no_template(self):
        stranger = structure_factories.UserFactory()
        response = self.create_offering(user=stranger)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ShippedPresetsCreateOfferingTest(BaseCreateOfferingTest):
    def load(self, key):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            f"{LIST_URL}load_preset/",
            {"presets": [key], "publish": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return models.OfferingTemplate.objects.get(
            name=offering_templates.load(key)["name"]
        )

    def test_slurm_preset(self):
        template = self.load("slurm")
        response = self.create_offering(template, parameters={"cluster_name": "alps"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        self.assertEqual(offering.backend_id, "alps")
        self.assertEqual(
            offering.plugin_options["username_generation_policy"], "anonymized"
        )
        self.assertEqual(
            sorted(offering.components.values_list("type", flat=True)), ["cpu", "mem"]
        )

    def test_slurm_preset_refuses_unknown_username_policy(self):
        template = self.load("slurm")
        response = self.create_offering(
            template,
            parameters={"cluster_name": "alps", "username_generation_policy": "x"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username_generation_policy", response.data["parameters"])

    def test_litellm_preset(self):
        template = self.load("litellm")
        response = self.create_offering(
            template, parameters={"api_url": "https://llm.example.com"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        token_cost = offering.components.get(type="token_cost")
        self.assertEqual(token_cost.spending_limit_periods["month"]["default"], 100)


@ddt
class ParameterDeclarationTest(test.APITestCase):
    def check(self, **fields):
        document = priced_document()
        document["parameters"][1].update(fields)
        return offering_templates.check_document(document, SITE_AGENT_OFFERING)

    def test_choices_and_pattern_are_kept(self):
        document = self.check(choices=["root", "hpc"], pattern="[a-z]+")
        parameter = document["parameters"][1]
        self.assertEqual(parameter["choices"], ["root", "hpc"])
        self.assertEqual(parameter["pattern"], "[a-z]+")

    def test_parameter_without_them_gains_no_keys(self):
        # A document saved before they existed keeps its hash.
        parameter = self.check()["parameters"][1]
        self.assertNotIn("choices", parameter)
        self.assertNotIn("pattern", parameter)

    @data(
        {"choices": []},
        {"choices": "root"},
        {"choices": ["root", 1]},
        {"choices": ["root", "root"]},
        {"choices": ["hpc"]},  # The default, root, is not among them.
        {"pattern": "["},
        {"pattern": "[0-9]+"},  # The default does not match.
        {"type": "boolean", "default": True, "choices": [True]},
        {"type": "integer", "default": 1, "pattern": "1"},
    )
    def test_malformed_declaration_is_refused(self, fields):
        with self.assertRaises(serializers.ValidationError):
            self.check(**fields)


@ddt
class ResolveParametersTest(test.APITestCase):
    PARAMETERS = [
        {"key": "name", "type": "string", "required": True, "default": None},
        {
            "key": "size",
            "type": "integer",
            "required": False,
            "default": 4,
            "choices": [4, 8],
        },
        {
            "key": "prefix",
            "type": "string",
            "required": False,
            "default": None,
            "pattern": "[a-z]+_",
        },
        {"key": "ratio", "type": "number", "required": False, "default": None},
    ]

    def errors(self, values):
        with self.assertRaises(serializers.ValidationError) as context:
            offering_templates.resolve_parameters(self.PARAMETERS, values)
        return context.exception.detail["parameters"]

    def test_defaults_fill_what_is_not_given(self):
        self.assertEqual(
            offering_templates.resolve_parameters(self.PARAMETERS, {"name": "x"}),
            {"name": "x", "size": 4, "prefix": None, "ratio": None},
        )

    def test_empty_string_is_no_value(self):
        self.assertIn("name", self.errors({"name": ""}))

    @data(
        ({"size": 6}, "size"),
        ({"size": True}, "size"),
        ({"size": "4"}, "size"),
        ({"prefix": "Hpc"}, "prefix"),
        ({"ratio": "0.5"}, "ratio"),
    )
    def test_value_that_does_not_fit_is_refused(self, case):
        values, key = case
        self.assertEqual(list(self.errors({"name": "x", **values})), [key])

    def test_number_takes_integers_and_decimals(self):
        for value in (1, 0.5):
            resolved = offering_templates.resolve_parameters(
                self.PARAMETERS, {"name": "x", "ratio": value}
            )
            self.assertEqual(resolved["ratio"], value)


class TemplateDiffTest(BaseCreateOfferingTest):
    def setUp(self):
        super().setUp()
        response = self.create_offering()
        self.offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])

    def diff(self, user=None):
        self.client.force_authenticate(user or self.owner)
        return self.client.get(
            f"{PROVIDER_OFFERINGS_URL}{self.offering.uuid.hex}/template_diff/"
        )

    def test_new_offering_has_no_drift(self):
        response = self.diff()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["created_version_differences"], [])
        self.assertEqual(response.data["latest_version_differences"], [])
        self.assertEqual(response.data["template_version"], 1)
        self.assertEqual(response.data["latest_version"], 1)

    def test_changed_offering_is_reported(self):
        component = self.offering.components.get()
        component.measured_unit = "hours"
        component.save()
        self.offering.backend_id = "other"
        self.offering.save()
        differences = self.diff().data["created_version_differences"]
        self.assertCountEqual(
            [(d["path"], d["template"], d["offering"]) for d in differences],
            [
                ("components[node].measured_unit", "node-hours", "hours"),
                ("offering.backend_id", "alps", "other"),
            ],
        )

    def test_added_component_is_reported(self):
        factories.OfferingComponentFactory(offering=self.offering, type="gpu")
        paths = [d["path"] for d in self.diff().data["created_version_differences"]]
        self.assertIn("components[gpu]", paths)

    def test_template_edit_shows_against_latest_version_only(self):
        document = copy.deepcopy(self.template.document)
        document["components"][0]["name"] = "Nodes"
        self.template.document = document
        self.template.save()
        response = self.diff()
        self.assertEqual(response.data["latest_version"], 2)
        self.assertEqual(response.data["created_version_differences"], [])
        self.assertEqual(
            response.data["latest_version_differences"],
            [
                {
                    "path": "components[node].name",
                    "template": "Nodes",
                    "offering": "Node hours",
                }
            ],
        )

    def test_offering_not_created_from_template_is_refused(self):
        self.offering.template = None
        self.offering.save()
        self.assertEqual(self.diff().status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_without_update_permission_cannot_see_it(self):
        stranger = structure_factories.UserFactory()
        self.assertIn(
            self.diff(stranger).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )


@ddt
class ParameterTargetTest(test.APITestCase):
    """A parameter fills only what the offering import reads."""

    def check(self, path):
        document = priced_document()
        document["offering"]["access_url"] = None
        document["components"][0]["spending_limit_periods"] = {}
        document["endpoints"] = [{"name": "API"}]
        document["parameters"].append(
            {"key": "extra", "label": "Extra", "paths": [path]}
        )
        return offering_templates.check_document(document, SITE_AGENT_OFFERING)

    @data(
        "offering.acess_url",
        "offering.name",
        "offering.type",
        "components.0.unit_factr",
        "plans.0.components.0.prise",
        "endpoints.0.uri",
    )
    def test_field_the_import_drops_is_refused(self, path):
        with self.assertRaises(serializers.ValidationError) as context:
            self.check(path)
        self.assertIn("does not read", str(context.exception.detail))

    @data(
        "offering.access_url",
        "components.0.unit_factor",
        "components.0.spending_limit_periods.month",
        "plans.0.unit_price",
        "plans.0.components.0.future_price",
        "endpoints.0.url",
    )
    def test_field_the_import_reads_is_accepted(self, path):
        self.check(path)

    def test_long_value_is_refused(self):
        parameters = [{"key": "name", "type": "string", "required": True}]
        with self.assertRaises(serializers.ValidationError):
            offering_templates.resolve_parameters(
                parameters,
                {"name": "a" * (offering_templates.PARAMETER_VALUE_MAX_LENGTH + 1)},
            )


class CreateOfferingReviewTest(BaseCreateOfferingTest):
    def test_organization_that_is_not_a_service_provider_is_refused(self):
        models.ServiceProvider.objects.filter(customer=self.customer).delete()
        for dry_run in (True, False):
            response = self.create_offering(dry_run=dry_run)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn("customer", response.data)
        self.assertFalse(models.Offering.objects.exists())

    def test_dry_run_sends_no_event_log(self):
        with self.assertNoLogs("waldur_core.logging.event_logger", level="INFO"):
            response = self.create_offering(dry_run=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_document_shows_agent_configuration_to_integration_editors(self):
        response = self.create_offering(dry_run=True)
        self.assertIn("agent_configuration", response.data["document"])

    def test_document_hides_agent_configuration_from_others(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        response = self.create_offering(dry_run=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn("agent_configuration", response.data["document"])
        self.assertIn("components", response.data["document"])


class TemplateDiffReviewTest(BaseCreateOfferingTest):
    def setUp(self):
        super().setUp()
        response = self.create_offering()
        self.offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])

    def diff(self, user=None):
        self.client.force_authenticate(user or self.owner)
        return self.client.get(
            f"{PROVIDER_OFFERINGS_URL}{self.offering.uuid.hex}/template_diff/"
        )

    def test_unpublished_template_is_compared_for_staff_only(self):
        self.template.is_published = False
        self.template.save()
        data = self.diff().data
        self.assertIsNone(data["latest_version"])
        self.assertIsNone(data["latest_version_differences"])
        self.assertEqual(data["created_version_differences"], [])
        data = self.diff(self.staff).data
        self.assertEqual(data["latest_version"], 1)
        self.assertEqual(data["latest_version_differences"], [])

    def test_other_category_is_no_drift(self):
        document = copy.deepcopy(self.template.document)
        document["offering"]["category_name"] = "Not the offering's category"
        template = make_template(document, name="With category")
        response = self.create_offering(template, name="Other")
        self.offering = models.Offering.objects.get(uuid=response.data["offering_uuid"])
        self.assertEqual(self.diff().data["created_version_differences"], [])

    def test_export_leaves_backend_id_out(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            f"{PROVIDER_OFFERINGS_URL}{self.offering.uuid.hex}/export_offering/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn("backend_id", response.data["export_data"]["offering"])

    def test_public_offering_does_not_name_its_template(self):
        self.offering.state = models.Offering.States.ACTIVE
        self.offering.save()
        response = self.client.get(
            f"/api/marketplace-public-offerings/{self.offering.uuid.hex}/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in ("template_uuid", "template_name", "template_version"):
            self.assertNotIn(field, response.data)


class DocumentDiffTest(test.APITestCase):
    def paths(self, expected, actual):
        return [d["path"] for d in offering_templates.document_diff(expected, actual)]

    def test_strings_are_text(self):
        self.assertEqual(
            self.paths(
                {"offering": {"backend_id": "01"}}, {"offering": {"backend_id": "1"}}
            ),
            ["offering.backend_id"],
        )

    def test_numbers_are_compared_by_value(self):
        expected = {
            "components": [{"type": "cpu", "unit_factor": 60000}],
            "plans": [
                {
                    "name": "Default",
                    "components": [{"component_type": "cpu", "price": "1.5"}],
                }
            ],
        }
        actual = {
            "components": [{"type": "cpu", "unit_factor": 60000.0}],
            "plans": [
                {
                    "name": "Default",
                    "components": [{"component_type": "cpu", "price": "1.500000"}],
                }
            ],
        }
        self.assertEqual(self.paths(expected, actual), [])

    def test_active_plan_is_compared_over_archived_one(self):
        expected = {"plans": [{"name": "Default", "unit": "month"}]}
        actual = {
            "plans": [
                {"name": "Default", "unit": "month", "archived": False},
                {"name": "Default", "unit": "day", "archived": True},
            ]
        }
        self.assertEqual(self.paths(expected, actual), [])
        actual["plans"].reverse()
        self.assertEqual(self.paths(expected, actual), [])
