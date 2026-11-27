import copy
import json
from pathlib import Path

from django.test import SimpleTestCase
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import consistency
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING
from waldur_mastermind.marketplace.offering_templates import load
from waldur_mastermind.marketplace.tests import factories, fixtures

PRESETS = Path(__file__).resolve().parents[2] / "marketplace" / "tests"
DWDI_PRESET = PRESETS / "offering_template_presets" / "slurm_dwdi.json"


def slurm_document():
    """A complete SLURM-only document that passes every rule."""
    return {
        "offering": {"type": SITE_AGENT_OFFERING, "backend_id": "alps"},
        "agent_configuration": {
            "order_processing_backend": "slurm",
            "membership_sync_backend": "slurm",
            "reporting_backend": "slurm",
            "backend_settings": {
                "cluster_name": "alps",
                "default_account": "root",
                "customer_prefix": "c_",
                "project_prefix": "p_",
                "allocation_prefix": "a_",
            },
        },
        "components": [
            {"type": "cpu", "billing_type": "usage", "unit_factor": 60000},
            {"type": "gres/gpu", "billing_type": "limit", "unit_factor": 60},
        ],
        "plans": [
            {
                "name": "Default",
                "components": [
                    {"component_type": "cpu", "price": 1},
                    {"component_type": "gres/gpu", "price": 2},
                ],
            }
        ],
    }


def dwdi_document():
    """SLURM for orders and members, DWDI compute for reporting, passing."""
    document = slurm_document()
    configuration = document["agent_configuration"]
    configuration["reporting_backend"] = "cscs-dwdi-compute"
    configuration["backend_settings"].update(
        {
            "cscs_dwdi_api_url": "https://dwdi.example.com",
            "cscs_dwdi_client_id": "waldur",
            "cscs_dwdi_oidc_token_url": "https://auth.example.com/token",
        }
    )
    configuration["backend_components"] = {"node": {"unit_factor_reporting": 1}}
    document["components"] = [
        {"type": "node", "billing_type": "usage", "unit_factor": 60}
    ]
    document["plans"][0]["components"] = [{"component_type": "node", "price": 1}]
    return document


def found(findings, rule, severity=None):
    return [
        item
        for item in findings
        if item["rule"] == rule and (severity is None or item["severity"] == severity)
    ]


class ConsistencyBasicsTest(SimpleTestCase):
    def test_complete_documents_pass(self):
        self.assertEqual(consistency.check_document(slurm_document()), [])
        self.assertEqual(consistency.check_document(dwdi_document()), [])

    def test_partial_documents_do_not_raise(self):
        for document in (
            {},
            {"offering": None, "components": "x", "plans": [None, 3]},
            {"agent_configuration": {"backend_settings": []}},
            {"components": [{"type": None}], "parameters": [None, {"paths": 3}]},
            [],
        ):
            consistency.check_document(document)

    def test_errors_come_first_and_has_errors(self):
        document = slurm_document()
        document["agent_configuration"]["backend_settings"].pop("cluster_name")
        document["plans"] = []
        findings = consistency.check_document(document)
        self.assertEqual(findings[0]["severity"], consistency.ERROR)
        self.assertEqual(findings[-1]["severity"], consistency.WARNING)
        self.assertTrue(consistency.has_errors(findings))
        self.assertFalse(consistency.has_errors(found(findings, "x")))
        self.assertEqual(set(findings[0]), {"rule", "severity", "field", "message"})

    def test_no_role_named_runs_slurm(self):
        # As the config generator does, an offering with no role backend runs
        # SLURM for all three; its legacy settings sit in plugin_options.
        document = slurm_document()
        document["agent_configuration"] = {}
        document["plugin_options"] = {"default_account": "root"}
        document["components"].append(
            {"type": "storage", "name": "Storage", "billing_type": "limit"}
        )
        findings = consistency.check_document(document)
        self.assertEqual(
            [item["field"] for item in found(findings, consistency.RULE_SLURM_TRES)],
            [f"components.{len(document['components']) - 1}.type"],
        )
        # The generator writes SLURM's settings with defaults: none is missing.
        self.assertEqual(found(findings, consistency.RULE_REQUIRED_SETTING), [])

    def test_custom_backend_gets_no_rules(self):
        document = slurm_document()
        configuration = document["agent_configuration"]
        configuration["custom_backends"] = ["slurm-site"]
        for field in (
            "order_processing_backend",
            "membership_sync_backend",
            "reporting_backend",
        ):
            configuration[field] = "slurm-site"
        configuration["backend_settings"] = {}
        document["components"][0]["type"] = "storage"
        document["plans"][0]["components"][0]["component_type"] = "storage"
        self.assertEqual(consistency.check_document(document), [])

    def test_shipped_presets_pass(self):
        for key in ("slurm", "litellm"):
            preset = load(key)
            self.assertEqual(consistency.check_document(preset["document"]), [], key)

    def test_slurm_dwdi_test_preset_passes(self):
        preset = json.loads(DWDI_PRESET.read_text())
        self.assertEqual(consistency.check_document(preset["document"]), [])


class SlurmTresRuleTest(SimpleTestCase):
    rule = consistency.RULE_SLURM_TRES

    def test_tres_names_pass(self):
        document = slurm_document()
        for component_type in ("mem", "node", "billing", "license/matlab"):
            document["components"].append(
                {"type": component_type, "billing_type": "usage"}
            )
            document["plans"][0]["components"].append(
                {"component_type": component_type}
            )
        self.assertEqual(found(consistency.check_document(document), self.rule), [])

    def test_other_names_are_errors(self):
        document = slurm_document()
        document["components"][0]["type"] = "storage"
        document["components"][1]["type"] = "gpu"
        findings = found(consistency.check_document(document), self.rule)
        self.assertEqual(
            [(item["severity"], item["field"]) for item in findings],
            [
                (consistency.ERROR, "components.0.type"),
                (consistency.ERROR, "components.1.type"),
            ],
        )

    def test_upper_case_is_a_warning(self):
        document = slurm_document()
        document["components"][1]["type"] = "GRES/GPU"
        findings = found(consistency.check_document(document), self.rule)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], consistency.WARNING)
        self.assertIn("gres/gpu", findings[0]["message"])

    def test_fixed_component_may_have_any_name(self):
        document = slurm_document()
        document["components"].append({"type": "support", "billing_type": "fixed"})
        document["plans"][0]["components"].append({"component_type": "support"})
        self.assertEqual(found(consistency.check_document(document), self.rule), [])

    def test_mapped_component_is_checked_by_its_targets(self):
        document = slurm_document()
        document["components"][0]["type"] = "node_hours"
        document["plans"][0]["components"][0]["component_type"] = "node_hours"
        document["agent_configuration"]["backend_components"] = {
            "node_hours": {"target_components": {"cpu": {"factor": 64}}}
        }
        self.assertEqual(found(consistency.check_document(document), self.rule), [])
        document["agent_configuration"]["backend_components"]["node_hours"][
            "target_components"
        ] = {"cores": {"factor": 64}}
        findings = found(consistency.check_document(document), self.rule)
        self.assertEqual(
            findings[0]["field"],
            "agent_configuration.backend_components.node_hours.target_components.cores",
        )

    def test_not_applied_without_slurm(self):
        document = slurm_document()
        configuration = document["agent_configuration"]
        for field in (
            "order_processing_backend",
            "membership_sync_backend",
            "reporting_backend",
        ):
            configuration[field] = "litellm"
        configuration["backend_settings"] = {"api_url": "https://llm.example.com"}
        document["offering"].pop("backend_id")
        document["components"][0]["type"] = "tokens"
        document["plans"][0]["components"][0]["component_type"] = "tokens"
        self.assertEqual(consistency.check_document(document), [])


class SlurmBillingTypeRuleTest(SimpleTestCase):
    rule = consistency.RULE_SLURM_BILLING_TYPE

    def test_limited_billing_types_pass(self):
        self.assertEqual(
            found(consistency.check_document(slurm_document()), self.rule), []
        )

    def test_fixed_tres_component_is_a_warning(self):
        document = slurm_document()
        document["components"][1]["billing_type"] = "fixed"
        findings = found(consistency.check_document(document), self.rule)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], consistency.WARNING)
        self.assertEqual(findings[0]["field"], "components.1.billing_type")


class DwdiNodeComponentRuleTest(SimpleTestCase):
    rule = consistency.RULE_DWDI_NODE_COMPONENT

    def test_node_component_passes(self):
        self.assertEqual(
            found(consistency.check_document(dwdi_document()), self.rule), []
        )

    def test_no_node_component_is_an_error(self):
        document = dwdi_document()
        document["components"][0]["type"] = "cpu"
        document["plans"][0]["components"][0]["component_type"] = "cpu"
        document["agent_configuration"].pop("backend_components")
        findings = found(consistency.check_document(document), self.rule)
        self.assertEqual(
            [(item["severity"], item["field"]) for item in findings],
            [(consistency.ERROR, "components")],
        )

    def test_usage_component_without_node_is_a_warning(self):
        document = dwdi_document()
        document["components"].append({"type": "cpu", "billing_type": "usage"})
        document["plans"][0]["components"].append({"component_type": "cpu"})
        findings = found(consistency.check_document(document), self.rule)
        self.assertEqual(
            [(item["severity"], item["field"]) for item in findings],
            [(consistency.WARNING, "components.1.type")],
        )

    def test_not_applied_to_slurm_reporting(self):
        document = slurm_document()
        self.assertEqual(found(consistency.check_document(document), self.rule), [])


class DwdiUnitFactorReportingRuleTest(SimpleTestCase):
    rule = consistency.RULE_DWDI_UNIT_FACTOR_REPORTING

    def test_set_passes(self):
        self.assertEqual(
            found(consistency.check_document(dwdi_document()), self.rule), []
        )

    def test_missing_is_an_error(self):
        document = dwdi_document()
        document["agent_configuration"].pop("backend_components")
        findings = found(consistency.check_document(document), self.rule)
        self.assertEqual(
            [(item["severity"], item["field"]) for item in findings],
            [
                (
                    consistency.ERROR,
                    "agent_configuration.backend_components.node.unit_factor_reporting",
                )
            ],
        )

    def test_not_applied_when_dwdi_runs_alone(self):
        document = dwdi_document()
        configuration = document["agent_configuration"]
        configuration.pop("backend_components")
        configuration.pop("order_processing_backend")
        configuration.pop("membership_sync_backend")
        self.assertEqual(found(consistency.check_document(document), self.rule), [])


class ClusterNameRuleTest(SimpleTestCase):
    rule = consistency.RULE_CLUSTER_NAME

    def findings(self, document):
        return [
            (item["severity"], item["field"])
            for item in found(consistency.check_document(document), self.rule)
        ]

    def test_equal_names_pass(self):
        self.assertEqual(self.findings(slurm_document()), [])

    def test_different_names_are_an_error(self):
        document = slurm_document()
        document["offering"]["backend_id"] = "eiger"
        self.assertEqual(
            self.findings(document), [(consistency.ERROR, "offering.backend_id")]
        )

    def test_empty_cluster_name_is_a_warning_in_cli_mode(self):
        document = slurm_document()
        document["agent_configuration"]["backend_settings"]["cluster_name"] = ""
        self.assertEqual(
            self.findings(document),
            [
                (
                    consistency.WARNING,
                    "agent_configuration.backend_settings.cluster_name",
                )
            ],
        )

    def test_empty_cluster_name_is_an_error_in_rest_mode(self):
        document = slurm_document()
        settings = document["agent_configuration"]["backend_settings"]
        settings.pop("cluster_name")
        settings["execution_mode"] = "rest"
        self.assertEqual(
            self.findings(document),
            [(consistency.ERROR, "agent_configuration.backend_settings.cluster_name")],
        )

    def test_empty_backend_id_is_an_error_with_dwdi(self):
        document = dwdi_document()
        document["offering"]["backend_id"] = None
        self.assertEqual(
            self.findings(document), [(consistency.ERROR, "offering.backend_id")]
        )
        document = slurm_document()
        document["offering"]["backend_id"] = None
        self.assertEqual(
            self.findings(document), [(consistency.WARNING, "offering.backend_id")]
        )

    def test_dwdi_cluster_setting_scopes_dwdi_without_backend_id(self):
        # DWDI falls back to cscs_dwdi_cluster when the offering has no
        # backend ID, so its usage is still scoped.
        document = dwdi_document()
        document["offering"]["backend_id"] = None
        document["agent_configuration"]["backend_settings"]["cscs_dwdi_cluster"] = (
            "alps"
        )
        self.assertEqual(
            self.findings(document), [(consistency.WARNING, "offering.backend_id")]
        )

    def test_one_parameter_filling_both_passes(self):
        document = slurm_document()
        document["offering"].pop("backend_id")
        document["agent_configuration"]["backend_settings"].pop("cluster_name")
        document["parameters"] = [
            {
                "key": "cluster_name",
                "paths": [
                    "offering.backend_id",
                    "agent_configuration.backend_settings.cluster_name",
                ],
            }
        ]
        self.assertEqual(self.findings(document), [])

    def test_parameter_filling_one_side_is_a_warning(self):
        document = slurm_document()
        document["offering"].pop("backend_id")
        document["parameters"] = [
            {"key": "backend_id", "paths": ["offering.backend_id"]}
        ]
        self.assertEqual(
            self.findings(document),
            [
                (
                    consistency.WARNING,
                    "agent_configuration.backend_settings.cluster_name",
                )
            ],
        )


class RequiredSettingRuleTest(SimpleTestCase):
    rule = consistency.RULE_REQUIRED_SETTING

    def test_set_passes(self):
        self.assertEqual(
            found(consistency.check_document(dwdi_document()), self.rule), []
        )

    def test_missing_is_an_error_per_setting(self):
        document = dwdi_document()
        document["agent_configuration"]["backend_settings"].pop("cscs_dwdi_api_url")
        findings = found(consistency.check_document(document), self.rule)
        self.assertEqual(
            [item["field"] for item in findings],
            ["agent_configuration.backend_settings.cscs_dwdi_api_url"],
        )
        self.assertEqual(findings[0]["severity"], consistency.ERROR)

    def test_empty_value_is_set(self):
        # The agent checks that a setting is there; an empty prefix is one.
        document = dwdi_document()
        document["agent_configuration"]["backend_settings"]["customer_prefix"] = ""
        self.assertEqual(found(consistency.check_document(document), self.rule), [])

    def test_slurm_settings_with_generator_defaults_are_never_missing(self):
        document = dwdi_document()
        settings = document["agent_configuration"]["backend_settings"]
        for name in ("default_account", "customer_prefix", "project_prefix"):
            settings.pop(name, None)
        self.assertEqual(found(consistency.check_document(document), self.rule), [])

    def test_secrets_and_parameter_targets_are_not_required(self):
        # cscs_dwdi_client_secret is required by DWDI but comes from the agent.
        document = dwdi_document()
        document["agent_configuration"]["backend_settings"].pop("default_account")
        document["parameters"] = [
            {
                "key": "default_account",
                "paths": ["agent_configuration.backend_settings.default_account"],
            }
        ]
        self.assertEqual(found(consistency.check_document(document), self.rule), [])


class PlanPricesRuleTest(SimpleTestCase):
    rule = consistency.RULE_PLAN_PRICES

    def test_complete_plan_passes(self):
        self.assertEqual(
            found(consistency.check_document(slurm_document()), self.rule), []
        )

    def test_unpriced_component_is_an_error(self):
        document = slurm_document()
        document["plans"][0]["components"].pop()
        findings = found(consistency.check_document(document), self.rule)
        self.assertEqual(
            [(item["severity"], item["field"]) for item in findings],
            [(consistency.ERROR, "plans.0.components")],
        )
        self.assertIn("gres/gpu", findings[0]["message"])

    def test_archived_plans_are_ignored(self):
        document = slurm_document()
        archived = copy.deepcopy(document["plans"][0])
        archived.update(archived=True, components=[])
        document["plans"].append(archived)
        self.assertEqual(found(consistency.check_document(document), self.rule), [])
        document["plans"] = [archived]
        findings = found(consistency.check_document(document), self.rule)
        self.assertEqual(findings[0]["field"], "plans")


class CheckConfigurationEndpointTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.backend_id = "alps"
        self.offering.agent_configuration = slurm_document()["agent_configuration"]
        self.offering.save()
        self.offering.components.all().delete()
        self.offering.plans.all().delete()
        component = factories.OfferingComponentFactory(
            offering=self.offering, type="cpu", billing_type="usage"
        )
        plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(plan=plan, component=component)
        # Test roles do not load permissions.yaml, which grants it to these.
        for role in (CustomerRole.OWNER, OfferingRole.MANAGER):
            role.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.url = factories.OfferingFactory.get_url(
            self.offering, "check_configuration"
        )

    def get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.url)

    def test_consistent_offering_has_no_findings(self):
        response = self.get(self.fixture.offering_owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data, [])

    def test_findings_are_listed(self):
        factories.OfferingComponentFactory(
            offering=self.offering, type="storage", billing_type="usage"
        )
        self.offering.backend_id = "eiger"
        self.offering.save()
        response = self.get(self.fixture.offering_owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rules = {(item["rule"], item["severity"]) for item in response.data}
        self.assertEqual(
            rules,
            {
                (consistency.RULE_SLURM_TRES, consistency.ERROR),
                (consistency.RULE_CLUSTER_NAME, consistency.ERROR),
                (consistency.RULE_PLAN_PRICES, consistency.ERROR),
            },
        )

    def test_legacy_offering_reads_plugin_options(self):
        self.offering.agent_configuration = {}
        self.offering.plugin_options = {
            "cluster_name": "alps",
            "default_account": "root",
            "customer_prefix": "c_",
            "project_prefix": "p_",
            "allocation_prefix": "a_",
        }
        self.offering.save()
        response = self.get(self.fixture.offering_owner)
        # cluster_name was never a legacy plugin option, so it is missing.
        self.assertEqual(
            [(item["rule"], item["field"]) for item in response.data],
            [
                (
                    consistency.RULE_CLUSTER_NAME,
                    "agent_configuration.backend_settings.cluster_name",
                )
            ],
        )

    def test_staff_and_offering_manager_may_check(self):
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        for user in (self.fixture.staff, manager):
            self.assertEqual(self.get(user).status_code, status.HTTP_200_OK)

    def test_consumers_may_not_check(self):
        for user in (self.fixture.owner, self.fixture.user):
            self.assertIn(
                self.get(user).status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )

    def test_other_offering_types_are_refused(self):
        self.offering.type = "Marketplace.Basic"
        self.offering.save()
        response = self.get(self.fixture.offering_owner)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TemplateFindingsTest(test.APITestCase):
    def test_staff_see_findings_of_the_template_document(self):
        document = slurm_document()
        document["components"][0]["type"] = "storage"
        document["plans"][0]["components"][0]["component_type"] = "storage"
        template = marketplace_models.OfferingTemplate.objects.create(
            name="t",
            offering_type=SITE_AGENT_OFFERING,
            document=document,
            schema_version=1,
        )
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        response = self.client.get(
            f"/api/marketplace-offering-templates/{template.uuid.hex}/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            {item["rule"] for item in response.data["configuration_findings"]},
            {consistency.RULE_SLURM_TRES},
        )
