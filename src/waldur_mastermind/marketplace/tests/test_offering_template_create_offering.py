import copy
from decimal import Decimal

from ddt import data, ddt
from rest_framework import serializers, status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, offering_templates
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING
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
