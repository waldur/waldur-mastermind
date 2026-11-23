import hashlib
import json
import string

import yaml
from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    SITE_AGENT_OFFERING,
    BillingTypes,
    OfferingStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.views import (
    ServiceProviderViewSet,
    _GeneratedAgentConfig,
)
from waldur_mastermind.marketplace_site_agent import (
    agent_configuration as site_agent_configuration,
)
from waldur_mastermind.marketplace_site_agent import config_builder
from waldur_mastermind.policy.models import SlurmPeriodicUsagePolicy


@ddt
class SiteAgentConfigGenerationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider

        # Create a SLURM offering for testing
        self.slurm_offering = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            state=OfferingStates.ACTIVE,
            customer=self.fixture.offering_customer,
            name="Test SLURM Cluster",
            plugin_options={
                "default_account": "test_account",
                "customer_prefix": "cust_",
                "project_prefix": "proj_",
                "hostname": "slurm.test.example.com",
                "qos_downscaled": "limited",
                "qos_paused": "suspended",
                "qos_default": "normal",
            },
        )

        # Create components for the offering
        self.cpu_component = factories.OfferingComponentFactory(
            offering=self.slurm_offering,
            type="cpu",
            name="CPU Hours",
            measured_unit="hours",
            unit_factor=60,
            billing_type=BillingTypes.USAGE,
        )
        self.mem_component = factories.OfferingComponentFactory(
            offering=self.slurm_offering,
            type="mem",
            name="Memory Hours",
            measured_unit="GB-hours",
            unit_factor=1024,
            billing_type=BillingTypes.USAGE,
        )

        # Grant permission for config generation
        CustomerRole.OWNER.add_permission(
            PermissionEnum.GET_SERVICE_PROVIDER_API_SECRET_CODE
        )

    def _get_url(self):
        return factories.ServiceProviderFactory.get_url(
            self.service_provider, "generate_site_agent_config"
        )

    def test_generate_config_returns_yaml(self):
        """Test that endpoint returns valid YAML."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(self.slurm_offering.uuid)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")

        # Verify it's valid YAML
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)
        self.assertIsInstance(config, dict)

    def test_generate_config_includes_placeholder_token(self):
        """Test that API token is a placeholder, not an actual secret."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(self.slurm_offering.uuid)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)

        offering_config = config["offerings"][0]
        self.assertEqual(offering_config["waldur_api_token"], "<YOUR_API_TOKEN_HERE>")

    def test_generate_config_includes_offering_details(self):
        """Test that offering details are correctly included."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(self.slurm_offering.uuid)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)

        offering_config = config["offerings"][0]
        self.assertEqual(offering_config["name"], "Test SLURM Cluster")
        self.assertEqual(
            offering_config["waldur_offering_uuid"], str(self.slurm_offering.uuid)
        )
        self.assertEqual(offering_config["order_processing_backend"], "slurm")
        self.assertEqual(offering_config["membership_sync_backend"], "slurm")
        self.assertEqual(offering_config["reporting_backend"], "slurm")

    def test_generate_config_includes_backend_settings(self):
        """Test that backend settings are extracted from plugin_options."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(self.slurm_offering.uuid)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)

        backend_settings = config["offerings"][0]["backend_settings"]
        self.assertEqual(backend_settings["default_account"], "test_account")
        self.assertEqual(backend_settings["customer_prefix"], "cust_")
        self.assertEqual(backend_settings["project_prefix"], "proj_")
        self.assertEqual(backend_settings["hostname"], "slurm.test.example.com")
        self.assertEqual(backend_settings["qos_downscaled"], "limited")
        self.assertEqual(backend_settings["qos_paused"], "suspended")

    def test_generate_config_includes_components(self):
        """Test that offering components are converted to backend_components."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(self.slurm_offering.uuid)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)

        components = config["offerings"][0]["backend_components"]
        self.assertIn("cpu", components)
        self.assertIn("mem", components)

        cpu_config = components["cpu"]
        self.assertEqual(cpu_config["measured_unit"], "hours")
        self.assertEqual(cpu_config["unit_factor"], 60)
        self.assertEqual(cpu_config["accounting_type"], "usage")
        self.assertEqual(cpu_config["label"], "CPU Hours")

    @data("user", "customer_support", "admin", "manager")
    def test_generate_config_requires_permission(self, user):
        """Test that unauthorized users cannot generate config."""
        user_obj = getattr(self.fixture, user)
        self.client.force_authenticate(user_obj)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(self.slurm_offering.uuid)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_generate_config_validates_offering_ownership(self):
        """Test that offerings must belong to the service provider."""
        # Create an offering owned by a different customer
        other_offering = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            state=OfferingStates.ACTIVE,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(other_offering.uuid)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_uuids", response.data)

    def test_generate_config_filters_slurm_offerings_only(self):
        """Test that only SLURM offerings are accepted."""
        # The fixture's default offering is BASIC_OFFERING, not SLURM
        non_slurm_offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            state=OfferingStates.ACTIVE,
            customer=self.fixture.offering_customer,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(non_slurm_offering.uuid)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_uuids", response.data)

    def test_generate_config_multiple_offerings(self):
        """Test configuration with multiple offerings."""
        # Create a second SLURM offering
        second_offering = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            state=OfferingStates.ACTIVE,
            customer=self.fixture.offering_customer,
            name="Second SLURM Cluster",
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {
                "offering_uuids": [
                    str(self.slurm_offering.uuid),
                    str(second_offering.uuid),
                ]
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)

        self.assertEqual(len(config["offerings"]), 2)
        offering_names = [o["name"] for o in config["offerings"]]
        self.assertIn("Test SLURM Cluster", offering_names)
        self.assertIn("Second SLURM Cluster", offering_names)

    def test_generate_config_custom_api_url(self):
        """Test that custom waldur_api_url is respected."""
        custom_url = "https://custom.waldur.example.com/api/"

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {
                "offering_uuids": [str(self.slurm_offering.uuid)],
                "waldur_api_url": custom_url,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)

        self.assertEqual(config["offerings"][0]["waldur_api_url"], custom_url)

    def test_generate_config_custom_timezone(self):
        """Test that custom timezone is respected."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {
                "offering_uuids": [str(self.slurm_offering.uuid)],
                "timezone": "Europe/Tallinn",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)

        self.assertEqual(config["timezone"], "Europe/Tallinn")

    def test_generate_config_without_policy(self):
        """Test config generation when no SLURM policy exists."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {
                "offering_uuids": [str(self.slurm_offering.uuid)],
                "include_policy_settings": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)

        # Policy settings should not be present if no policy exists
        self.assertNotIn("policy_settings", config["offerings"][0])

    def test_generate_config_exclude_policy_settings(self):
        """Test config generation with policy settings excluded."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {
                "offering_uuids": [str(self.slurm_offering.uuid)],
                "include_policy_settings": False,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)

        # Policy settings should not be present when excluded
        self.assertNotIn("policy_settings", config["offerings"][0])

    def test_generate_config_has_yaml_header_comments(self):
        """Test that generated YAML includes instructional header."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(self.slurm_offering.uuid)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")

        # Check for header comments
        self.assertIn("# Waldur Site Agent Configuration", content)
        self.assertIn("# Generated:", content)
        self.assertIn("waldur_api_token", content)

    def test_generate_config_returns_plain_text_for_ui(self):
        """Test that response is plain text for UI display."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(self.slurm_offering.uuid)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        # Verify no attachment header - content should be displayable in UI
        self.assertNotIn("Content-Disposition", response)

    def test_generate_config_requires_at_least_one_offering(self):
        """Test that at least one offering UUID is required."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": []},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_generate_config_keeps_the_hostname_placeholder_without_roles(self):
        """An offering with no agent roles set is generated as it always was."""
        # Create offering without hostname in plugin_options
        offering_no_hostname = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            state=OfferingStates.ACTIVE,
            customer=self.fixture.offering_customer,
            name="SLURM No Hostname",
            plugin_options={},
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(offering_no_hostname.uuid)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)

        self.assertEqual(
            config["offerings"][0]["backend_settings"]["hostname"], "<YOUR_SLURM_HOST>"
        )
        self.assertIn("hostname: Your SLURM cluster hostname", content)


class SiteAgentConfigWithPolicyTest(test.APITestCase):
    """Tests for site agent config generation with SLURM policy."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider

        # Create a SLURM offering
        self.slurm_offering = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            state=OfferingStates.ACTIVE,
            customer=self.fixture.offering_customer,
            name="SLURM with Policy",
        )

        # Grant permission
        CustomerRole.OWNER.add_permission(
            PermissionEnum.GET_SERVICE_PROVIDER_API_SECRET_CODE
        )

    def _get_url(self):
        return factories.ServiceProviderFactory.get_url(
            self.service_provider, "generate_site_agent_config"
        )

    def test_generate_config_includes_policy_settings(self):
        """Test that SLURM policy settings are included when policy exists."""
        # Create a SLURM policy for the offering
        from waldur_mastermind.policy.models import SlurmPeriodicUsagePolicy

        SlurmPeriodicUsagePolicy.objects.create(
            scope=self.slurm_offering,
            actions="notify_organization_owners",
            limit_type="GrpTRESMins",
            tres_billing_enabled=True,
            tres_billing_weights={"CPU": 0.015625, "Mem": 0.001953125},
            carryover_factor=15,
            grace_ratio=0.2,
            carryover_enabled=True,
            raw_usage_reset=True,
            qos_strategy="progressive",
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {
                "offering_uuids": [str(self.slurm_offering.uuid)],
                "include_policy_settings": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        config = yaml.safe_load(content)

        policy_settings = config["offerings"][0].get("policy_settings")
        self.assertIsNotNone(policy_settings)
        self.assertEqual(policy_settings["limit_type"], "GrpTRESMins")
        self.assertEqual(policy_settings["tres_billing_enabled"], True)
        self.assertEqual(policy_settings["carryover_factor"], 15)
        self.assertEqual(policy_settings["grace_ratio"], 0.2)
        self.assertEqual(policy_settings["carryover_enabled"], True)
        self.assertEqual(policy_settings["raw_usage_reset"], True)
        self.assertEqual(policy_settings["qos_strategy"], "progressive")
        self.assertEqual(policy_settings["tres_billing_weights"]["CPU"], 0.015625)


class SiteAgentConfigFromAgentConfigurationTest(test.APITestCase):
    """The generated configuration follows the offering's agent configuration."""

    setUp = SiteAgentConfigGenerationTest.setUp
    _get_url = SiteAgentConfigGenerationTest._get_url

    def _generate(self, **extra):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {"offering_uuids": [str(self.slurm_offering.uuid)], **extra},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")
        return content, yaml.safe_load(content)["offerings"][0]

    def _use_dwdi_reporting(self, backend_settings=None):
        self.slurm_offering.agent_configuration = {
            "order_processing_backend": "slurm",
            "membership_sync_backend": "slurm",
            "reporting_backend": "cscs-dwdi-compute",
            "backend_settings": backend_settings
            or {
                "default_account": "root",
                "customer_prefix": "c_",
                "project_prefix": "p_",
                "allocation_prefix": "a_",
                "cluster_name": "alpha",
                "cscs_dwdi_api_url": "https://dwdi.example.com",
                "cscs_dwdi_client_id": "waldur",
                "cscs_dwdi_oidc_token_url": "https://auth.example.com/token",
            },
            "backend_components": {"cpu": {"unit_factor_reporting": 1}},
        }
        self.slurm_offering.save()

    def test_roles_come_from_agent_configuration(self):
        self._use_dwdi_reporting()

        _, offering_config = self._generate()

        self.assertEqual(offering_config["backend_type"], "slurm")
        self.assertEqual(offering_config["order_processing_backend"], "slurm")
        self.assertEqual(offering_config["reporting_backend"], "cscs-dwdi-compute")

    def test_agent_settings_and_component_fields_are_generated(self):
        self._use_dwdi_reporting()

        _, offering_config = self._generate()

        settings = offering_config["backend_settings"]
        self.assertEqual(settings["cluster_name"], "alpha")
        self.assertEqual(settings["cscs_dwdi_api_url"], "https://dwdi.example.com")
        self.assertEqual(settings["default_account"], "root")
        components = offering_config["backend_components"]
        self.assertEqual(components["cpu"]["unit_factor_reporting"], 1)
        self.assertEqual(components["cpu"]["unit_factor"], 60)
        self.assertNotIn("unit_factor_reporting", components["mem"])

    def test_required_secrets_are_placeholders_and_listed(self):
        self._use_dwdi_reporting()

        content, offering_config = self._generate()

        # An empty value with the secret named beside it.
        self.assertEqual(
            offering_config["backend_settings"]["cscs_dwdi_client_secret"], ""
        )
        self.assertIn(
            "cscs_dwdi_client_secret: ''  # SECRET: cscs-dwdi-compute\n", content
        )
        self.assertEqual(offering_config["waldur_api_token"], "")
        self.assertIn("waldur_api_token: ''  # SECRET: Waldur API token", content)
        self.assertIn(
            "backend_settings.cscs_dwdi_client_secret (cscs-dwdi-compute)", content
        )

    def test_secret_values_never_reach_the_output(self):
        # Written straight to the database, past the serializer that refuses it.
        self._use_dwdi_reporting()
        self.slurm_offering.agent_configuration["backend_settings"][
            "cscs_dwdi_client_secret"
        ] = "leaked-secret"
        self.slurm_offering.secret_options = {"cscs_dwdi_client_secret": "leaked"}
        self.slurm_offering.save()

        content, _ = self._generate()

        self.assertNotIn("leaked", content)

    def test_offering_without_agent_configuration_is_unchanged(self):
        content, offering_config = self._generate()

        # The only backend secret is that of SLURM's optional ldap block, which
        # is listed but not created.
        self.assertIn(
            "backend_settings.ldap.bind_password (slurm, required if its block is set)",
            content,
        )
        self.assertNotIn("ldap", offering_config["backend_settings"])
        self.assertEqual(
            list(offering_config["backend_settings"]),
            [
                "default_account",
                "customer_prefix",
                "project_prefix",
                "allocation_prefix",
                "qos_downscaled",
                "qos_paused",
                "qos_default",
                "hostname",
                "enable_user_homedir_account_creation",
            ],
        )
        self.assertEqual(offering_config["reporting_backend"], "slurm")
        self.assertEqual(offering_config["waldur_api_token"], "<YOUR_API_TOKEN_HERE>")
        # Catalogue defaults are left out, as generated configurations did.
        self.assertNotIn("execution_mode", offering_config["backend_settings"])

    def test_unset_roles_stay_off_once_any_role_is_set(self):
        self.slurm_offering.agent_configuration = {
            "reporting_backend": "cscs-dwdi-compute",
            "backend_settings": {"cscs_dwdi_api_url": "https://dwdi.example.com"},
        }
        self.slurm_offering.save()

        _, offering_config = self._generate()

        self.assertNotIn("order_processing_backend", offering_config)
        self.assertNotIn("membership_sync_backend", offering_config)
        self.assertEqual(offering_config["backend_type"], "cscs-dwdi-compute")
        # No SLURM defaults for an offering that does not run SLURM orders.
        self.assertNotIn("hostname", offering_config["backend_settings"])

    def test_slurm_defaults_come_with_any_slurm_role(self):
        self.slurm_offering.agent_configuration = {
            "membership_sync_backend": "slurm",
            "reporting_backend": "cscs-dwdi-compute",
            "backend_settings": {"cscs_dwdi_api_url": "https://dwdi.example.com"},
        }
        self.slurm_offering.save()

        _, offering_config = self._generate()

        self.assertNotIn("order_processing_backend", offering_config)
        settings = offering_config["backend_settings"]
        for key in ("default_account", "customer_prefix", "qos_default"):
            self.assertIn(key, settings)

    def test_required_secret_of_a_missing_block_is_only_listed(self):
        settings = {"cluster_name": "alpha"}
        generated = _GeneratedAgentConfig("comment")

        for backend, path in (("ldap", "ldap.bind_password"), ("other", "token")):
            ServiceProviderViewSet._add_secret_placeholder(
                settings,
                self.slurm_offering,
                backend,
                path,
                site_agent_configuration.REQUIRED,
                generated,
            )

        self.assertEqual(set(settings), {"cluster_name", "token"})
        self.assertEqual(
            [(s["field"], s["in_file"]) for s in generated.secrets],
            [
                ("backend_settings.ldap.bind_password", False),
                ("backend_settings.token", True),
            ],
        )

    def test_nested_secret_of_a_configured_block_gets_a_placeholder(self):
        self._use_dwdi_reporting()
        self.slurm_offering.agent_configuration["username_management_backend"] = "ldap"
        self.slurm_offering.agent_configuration["backend_settings"]["ldap"] = {
            "uri": "ldap://ldap.example.com"
        }
        self.slurm_offering.save()

        content, offering_config = self._generate()

        ldap = offering_config["backend_settings"]["ldap"]
        self.assertEqual(ldap["bind_password"], "")
        # Listed under the first backend that declares it: SLURM declares its
        # ldap block too.
        self.assertIn("bind_password: ''  # SECRET: slurm", content)
        self.assertIn("backend_settings.ldap.bind_password (slurm)", content)
        self.assertNotIn("ldap.bind_password (slurm, optional)", content)

    def test_env_format_names_a_variable_for_each_secret(self):
        self._use_dwdi_reporting()
        self.slurm_offering.name = "Alpha HPC"
        self.slurm_offering.save()

        content, offering_config = self._generate(secret_format="env")

        self.assertEqual(
            offering_config["backend_settings"]["cscs_dwdi_client_secret"],
            "${ALPHA_HPC_CSCS_DWDI_CLIENT_SECRET}",
        )
        self.assertEqual(
            offering_config["waldur_api_token"], "${ALPHA_HPC_WALDUR_API_TOKEN}"
        )
        self.assertIn("envsubst", content)
        self.assertIn("as ${ALPHA_HPC_CSCS_DWDI_CLIENT_SECRET}", content)

    def test_offerings_of_one_name_get_their_own_variables(self):
        twin = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            state=OfferingStates.ACTIVE,
            customer=self.fixture.offering_customer,
            name=self.slurm_offering.name,
        )
        self.client.force_authenticate(self.fixture.offering_owner)

        response = self.client.post(
            self._get_url(),
            {
                "offering_uuids": [str(self.slurm_offering.uuid), str(twin.uuid)],
                "secret_format": "env",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        tokens = [
            offering["waldur_api_token"]
            for offering in yaml.safe_load(response.content)["offerings"]
        ]
        self.assertEqual(len(set(tokens)), 2, tokens)

    def test_env_secrets_keep_their_value_once_filled_in(self):
        # Filled in by envsubst, a secret lands in the YAML as written; an
        # unquoted reference would cut it at " #" or fail to load on "*" or ": ".
        self._use_dwdi_reporting()
        self.slurm_offering.name = "Alpha HPC"
        self.slurm_offering.save()
        content, _ = self._generate(secret_format="env")

        for secret in ("p@ss #42", "*abc", "a: b", "%x", r"back\slash"):
            filled = string.Template(content).safe_substitute(
                ALPHA_HPC_CSCS_DWDI_CLIENT_SECRET=secret,
                ALPHA_HPC_WALDUR_API_TOKEN="token",
            )
            settings = yaml.safe_load(filled)["offerings"][0]["backend_settings"]
            self.assertEqual(settings["cscs_dwdi_client_secret"], secret)

    def test_stored_text_never_takes_a_secret_placeholder(self):
        # Someone who can edit the integration but not the agent host must not
        # get a secret put into a setting the agent sends elsewhere, such as a
        # URL, by writing the placeholder's name into it.
        planted = "".join(f"__waldur_agent_secret_{n}__" for n in range(5))
        self._use_dwdi_reporting()
        settings = self.slurm_offering.agent_configuration["backend_settings"]
        settings["cscs_dwdi_api_url"] = f"https://evil.example/{planted}"
        self.slurm_offering.save()
        self.cpu_component.name = planted
        self.cpu_component.save()

        for secret_format in ("comment", "env"):
            content, offering_config = self._generate(secret_format=secret_format)

            self.assertEqual(
                offering_config["backend_settings"]["cscs_dwdi_api_url"],
                f"https://evil.example/{planted}",
            )
            self.assertEqual(
                offering_config["backend_components"]["cpu"]["label"], planted
            )
            # The real placeholders are still put where they belong.
            expected = "" if secret_format == "comment" else "${"
            self.assertTrue(offering_config["waldur_api_token"].startswith(expected))
            self.assertTrue(
                offering_config["backend_settings"][
                    "cscs_dwdi_client_secret"
                ].startswith(expected)
            )

    def test_component_limit_is_generated(self):
        # The SLURM backend caps an allocation at limit * unit_factor for each
        # usage component with a limit; without it the cap is lost.
        self.cpu_component.limit_amount = 100
        self.cpu_component.save()
        self._use_dwdi_reporting()

        _, offering_config = self._generate()

        components = offering_config["backend_components"]
        self.assertEqual(components["cpu"]["limit"], 100)
        self.assertNotIn("limit", components["mem"])

    def test_accounting_type_follows_the_billing_type(self):
        # The SLURM backend multiplies a prepaid limit by the subscription's
        # months only for "one"; written as "limit", a prepaid allocation
        # gets a single month's budget.
        self.cpu_component.billing_type = BillingTypes.ONE_TIME
        self.cpu_component.is_prepaid = True
        self.cpu_component.save()
        self.mem_component.billing_type = BillingTypes.LIMIT
        self.mem_component.save()
        factories.OfferingComponentFactory(
            offering=self.slurm_offering,
            type="storage",
            billing_type=BillingTypes.FIXED,
        )

        _, offering_config = self._generate()

        components = offering_config["backend_components"]
        self.assertEqual(components["cpu"]["accounting_type"], "one")
        self.assertEqual(components["mem"]["accounting_type"], "limit")
        self.assertEqual(components["storage"]["accounting_type"], "limit")

    def test_env_format_applies_to_offerings_without_roles_too(self):
        _, offering_config = self._generate(secret_format="env")

        self.assertTrue(offering_config["waldur_api_token"].startswith("${"))

    def test_unknown_secret_format_is_refused(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self._get_url(),
            {
                "offering_uuids": [str(self.slurm_offering.uuid)],
                "secret_format": "vault",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_secrets_are_listed_as_data(self):
        self._use_dwdi_reporting()
        self.client.force_authenticate(self.fixture.offering_owner)
        url = factories.ServiceProviderFactory.get_url(
            self.fixture.service_provider, "site_agent_config_secrets"
        )

        response = self.client.post(
            url,
            {"offering_uuids": [str(self.slurm_offering.uuid)], "secret_format": "env"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        by_field = {secret["field"]: secret for secret in response.data}
        self.assertEqual(
            set(by_field),
            {
                "waldur_api_token",
                "backend_settings.cscs_dwdi_client_secret",
                # SLURM's optional ldap block, listed though not configured.
                "backend_settings.ldap.bind_password",
            },
        )
        dwdi = by_field["backend_settings.cscs_dwdi_client_secret"]
        self.assertEqual(dwdi["backend"], "cscs-dwdi-compute")
        self.assertEqual(dwdi["requirement"], "required")
        self.assertTrue(dwdi["in_file"])
        self.assertTrue(dwdi["env_var"].endswith("_CSCS_DWDI_CLIENT_SECRET"))
        self.assertIsNone(by_field["waldur_api_token"]["backend"])

    def test_catalogue_defaults_fill_in_what_the_configuration_leaves_out(self):
        self._use_dwdi_reporting()

        _, offering_config = self._generate()

        settings = offering_config["backend_settings"]
        self.assertEqual(settings["execution_mode"], "cli")
        self.assertEqual(settings["default_account_policy"], "common")
        # What the generator or the offering sets wins over a catalogue default.
        self.assertIs(settings["enable_user_homedir_account_creation"], False)
        self.assertEqual(settings["customer_prefix"], "c_")

    def test_event_settings_are_generated(self):
        self._use_dwdi_reporting()
        self.slurm_offering.agent_configuration["event_settings"] = {
            "stomp_enabled": True,
            "stomp_ws_host": "waldur.example.com",
            "stomp_ws_port": 443,
            "stomp_ws_path": "/rmqws-stomp",
        }
        self.slurm_offering.save()

        _, offering_config = self._generate()

        self.assertIs(offering_config["stomp_enabled"], True)
        # In the order the agent documents them, whatever order they were stored in.
        keys = list(offering_config)
        self.assertLess(keys.index("stomp_ws_port"), keys.index("stomp_ws_path"))
        self.assertEqual(offering_config["stomp_ws_host"], "waldur.example.com")
        self.assertEqual(offering_config["stomp_ws_port"], 443)
        self.assertNotIn("websocket_use_tls", offering_config)

    def test_unset_hostname_is_left_out_once_roles_are_set(self):
        self._use_dwdi_reporting()
        self.slurm_offering.plugin_options.pop("hostname", None)
        self.slurm_offering.save()

        content, offering_config = self._generate()

        self.assertNotIn("hostname", offering_config["backend_settings"])
        self.assertNotIn("hostname: Your SLURM cluster hostname", content)

    def test_offering_name_cannot_break_out_of_the_header(self):
        self._use_dwdi_reporting()
        self.slurm_offering.name = "Cluster\nglobal_proxy: http://evil:8080"
        self.slurm_offering.save()

        content, _ = self._generate()

        config = yaml.safe_load(content)
        self.assertNotIn("global_proxy", config)

    def test_agent_fields_never_replace_offering_component_fields(self):
        # Written straight to the database, past the serializer that refuses it.
        self._use_dwdi_reporting()
        self.slurm_offering.agent_configuration["backend_components"] = {
            "cpu": {"unit_factor": 1, "unit_factor_reporting": 2},
            "mem": "not a mapping",
        }
        self.slurm_offering.save()

        _, offering_config = self._generate()

        components = offering_config["backend_components"]
        self.assertEqual(components["cpu"]["unit_factor"], 60)
        self.assertEqual(components["cpu"]["unit_factor_reporting"], 2)
        self.assertEqual(components["mem"]["unit_factor"], 1024)


class SiteAgentOfferingConfigTest(test.APITestCase):
    """The configuration a site agent loads for its offering (#1287)."""

    setUp_generator = SiteAgentConfigGenerationTest.setUp
    _use_dwdi_reporting = SiteAgentConfigFromAgentConfigurationTest._use_dwdi_reporting

    def setUp(self):
        self.setUp_generator()
        # Test roles do not load permissions.yaml, which grants it to these.
        for role in (CustomerRole.OWNER, OfferingRole.MANAGER):
            role.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.offering_manager = structure_factories.UserFactory()
        self.slurm_offering.add_user(self.offering_manager, OfferingRole.MANAGER)

    def _get(self, user=None, offering=None):
        self.client.force_authenticate(user or self.offering_manager)
        return self.client.get(
            factories.OfferingFactory.get_url(
                offering or self.slurm_offering, "site_agent_config"
            )
        )

    def _config(self):
        response = self._get()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return json.loads(response.content)

    def _generated(self, **extra):
        """The generator's offering block and its secrets, for the same offering."""
        self.client.force_authenticate(self.fixture.offering_owner)
        body = {"offering_uuids": [str(self.slurm_offering.uuid)], **extra}
        response = self.client.post(
            factories.ServiceProviderFactory.get_url(
                self.service_provider, "generate_site_agent_config"
            ),
            body,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        block = yaml.safe_load(response.content)["offerings"][0]
        response = self.client.post(
            factories.ServiceProviderFactory.get_url(
                self.service_provider, "site_agent_config_secrets"
            ),
            body,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return block, response.data

    def _generated_without_placeholders(self):
        """The generator's offering block minus bootstrap fields and placeholders."""
        block, secrets = self._generated()
        for field in config_builder.BOOTSTRAP_FIELDS:
            del block[field]
        for secret in secrets:
            if not secret["in_file"] or secret["field"] == "waldur_api_token":
                continue
            *parents, leaf = secret["field"].split(".")
            node = block
            for part in parents:
                node = node[part]
            del node[leaf]
        settings = block["backend_settings"]
        if settings.get("hostname") == config_builder.LEGACY_HOSTNAME_PLACEHOLDER:
            del settings["hostname"]
        return block

    def _configure_everything(self):
        """Roles, a required secret's block, event and agent settings, a policy."""
        self._use_dwdi_reporting()
        configuration = self.slurm_offering.agent_configuration
        configuration["username_management_backend"] = "ldap"
        configuration["backend_settings"]["ldap"] = {"uri": "ldap://ldap.example.com"}
        configuration["event_settings"] = {
            "stomp_enabled": True,
            "stomp_ws_host": "waldur.example.com",
            "stomp_ws_port": 443,
        }
        configuration["agent_settings"] = {
            "resource_import_enabled": True,
            "omit_anomalous_usage_components": False,
        }
        self.slurm_offering.save()
        self.cpu_component.limit_amount = 100
        self.cpu_component.save()
        SlurmPeriodicUsagePolicy.objects.create(
            scope=self.slurm_offering,
            actions="notify_organization_owners",
            limit_type="GrpTRESMins",
            tres_billing_weights={"CPU": 0.015625},
            grace_ratio=0.2,
        )

    def test_offering_manager_reads_the_configuration(self):
        config = self._config()

        self.assertEqual(set(config), {"offering", "secrets", "version"})
        self.assertEqual(config["offering"]["name"], "Test SLURM Cluster")

    def test_who_may_read_it(self):
        provider_manager = structure_factories.UserFactory()
        self.service_provider.add_user(provider_manager, ServiceProviderRole.MANAGER)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_INTEGRATION
        )
        support = structure_factories.UserFactory()
        self.slurm_offering.customer.add_user(support, CustomerRole.SUPPORT)
        for label, user, allowed in (
            ("offering manager", self.offering_manager, True),
            ("owner", self.fixture.offering_owner, True),
            ("service provider manager", provider_manager, True),
            ("staff", structure_factories.UserFactory(is_staff=True), True),
            ("customer support", support, False),
            ("unrelated user", structure_factories.UserFactory(), False),
            ("consumer", self.fixture.owner, False),
        ):
            with self.subTest(label):
                response = self._get(user)

                if allowed:
                    self.assertEqual(response.status_code, status.HTTP_200_OK)
                else:
                    self.assertIn(
                        response.status_code,
                        (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                    )

    def test_offering_manager_without_the_permission_is_refused(self):
        OfferingRole.MANAGER.delete_permission(
            PermissionEnum.UPDATE_OFFERING_INTEGRATION
        )

        response = self._get()

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_other_offering_types_are_refused(self):
        offering = factories.OfferingFactory(
            type=BASIC_OFFERING, customer=self.fixture.offering_customer
        )

        response = self._get(self.fixture.offering_owner, offering)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_is_the_generators_block_without_placeholders(self):
        for label in ("legacy", "legacy without hostname", "configured"):
            with self.subTest(label):
                if label == "legacy without hostname":
                    self.slurm_offering.plugin_options.pop("hostname")
                    self.slurm_offering.save()
                elif label == "configured":
                    self._configure_everything()

                self.assertEqual(
                    self._config()["offering"], self._generated_without_placeholders()
                )

    def test_agent_settings_are_written_at_the_offering_level(self):
        self._configure_everything()

        block, _ = self._generated()
        offering = self._config()["offering"]

        for config in (block, offering):
            self.assertIs(config["resource_import_enabled"], True)
            self.assertIs(config["omit_anomalous_usage_components"], False)
            self.assertNotIn("username_reconciliation_enabled", config)
            self.assertNotIn("agent_settings", config)
        keys = list(block)
        self.assertLess(
            keys.index("stomp_ws_port"), keys.index("resource_import_enabled")
        )
        self.assertLess(
            keys.index("resource_import_enabled"), keys.index("backend_settings")
        )

    def test_secrets_are_listed_not_included(self):
        self._configure_everything()

        config = self._config()

        self.assertEqual(
            config["secrets"],
            [
                # In the order of the backends' roles.
                {
                    "field": "backend_settings.ldap.bind_password",
                    "backend": "slurm",
                    "requirement": "required",
                },
                {
                    "field": "backend_settings.cscs_dwdi_client_secret",
                    "backend": "cscs-dwdi-compute",
                    "requirement": "required",
                },
                # Optional: listed though its block is not set.
                {
                    "field": "backend_settings.ldap.welcome_email.smtp_password",
                    "backend": "ldap",
                    "requirement": "optional",
                },
            ],
        )
        settings = config["offering"]["backend_settings"]
        self.assertNotIn("cscs_dwdi_client_secret", settings)
        self.assertEqual(settings["ldap"], {"uri": "ldap://ldap.example.com"})
        # The same secrets as the generator's, apart from the API token.
        _, generated = self._generated()
        self.assertEqual(
            sorted(
                (s["field"], s["backend"], s["requirement"])
                for s in generated
                if s["field"] != "waldur_api_token"
            ),
            sorted(
                (s["field"], s["backend"], s["requirement"]) for s in config["secrets"]
            ),
        )

    def test_legacy_offering_lists_the_optional_ldap_secret(self):
        config = self._config()

        self.assertEqual(
            config["secrets"],
            [
                {
                    "field": "backend_settings.ldap.bind_password",
                    "backend": "slurm",
                    "requirement": "required_with_parent",
                }
            ],
        )

    def test_no_secret_value_or_placeholder_is_returned(self):
        # Written straight to the database, past the serializers that refuse it.
        self._configure_everything()
        configuration = self.slurm_offering.agent_configuration
        configuration["backend_settings"]["cscs_dwdi_client_secret"] = "leaked-1"
        configuration["backend_settings"]["ldap"]["bind_password"] = "leaked-2"
        self.slurm_offering.secret_options = {
            "cscs_dwdi_client_secret": "leaked-3",
            "api_token": "leaked-4",
        }
        self.slurm_offering.plugin_options["password"] = "leaked-5"
        self.slurm_offering.save()

        content = self._get().content.decode()

        for text in ("leaked", "__waldur_agent_secret", "SECRET", "${", "<YOUR_"):
            self.assertNotIn(text, content)
        for field in config_builder.BOOTSTRAP_FIELDS:
            self.assertNotIn(field, content)

    def test_component_secrets_are_never_returned(self):
        # Written straight to the database, past the serializer that refuses it.
        self._use_dwdi_reporting()
        self.slurm_offering.agent_configuration["backend_components"] = {
            "cpu": {
                "unit_factor_reporting": 1,
                "api_token": "leaked-1",
                "nested": {"password": "leaked-2"},
            }
        }
        self.slurm_offering.save()

        block, _ = self._generated()
        offering = self._config()["offering"]

        for config in (block, offering):
            cpu = config["backend_components"]["cpu"]
            self.assertEqual(cpu["unit_factor_reporting"], 1)
            self.assertNotIn("api_token", cpu)
            self.assertEqual(cpu["nested"], {})
        self.assertNotIn("leaked", self._get().content.decode())
        self.assertEqual(offering, self._generated_without_placeholders())

    def test_version_is_the_hash_of_the_canonical_offering(self):
        config = self._config()

        canonical = json.dumps(
            config["offering"], sort_keys=True, separators=(",", ":")
        )
        self.assertEqual(
            config["version"], hashlib.sha256(canonical.encode()).hexdigest()
        )

    def test_version_changes_when_and_only_when_the_offering_does(self):
        first = self._config()

        self.assertEqual(self._config(), first)

        # Not part of what the agent loads.
        self.slurm_offering.description = "Another description"
        self.slurm_offering.secret_options = {"cscs_dwdi_client_secret": "x"}
        self.slurm_offering.save()
        self.assertEqual(self._config()["version"], first["version"])

        self.cpu_component.unit_factor = 30
        self.cpu_component.save()
        second = self._config()
        self.assertNotEqual(second["offering"], first["offering"])
        self.assertNotEqual(second["version"], first["version"])

        self.slurm_offering.agent_configuration = {
            "agent_settings": {"omit_anomalous_usage_components": True}
        }
        self.slurm_offering.save()
        third = self._config()
        self.assertIs(third["offering"]["omit_anomalous_usage_components"], True)
        self.assertNotEqual(third["version"], second["version"])

    def test_unset_event_setting_stored_as_null_does_not_change_the_version(self):
        # Saving a configuration back as it was read stores the unset setting
        # as null; the agent reads both as unset.
        self.slurm_offering.agent_configuration = {
            "event_settings": {"stomp_enabled": False}
        }
        self.slurm_offering.save()
        first = self._config()

        self.slurm_offering.agent_configuration = {
            "event_settings": {
                "stomp_enabled": False,
                "stomp_membership_sync_enabled": None,
            }
        }
        self.slurm_offering.save()
        second = self._config()

        self.assertNotIn("stomp_membership_sync_enabled", second["offering"])
        self.assertEqual(second["version"], first["version"])

    def test_legacy_offering_runs_slurm_with_its_defaults(self):
        self.slurm_offering.plugin_options = {}
        self.slurm_offering.save()

        offering = self._config()["offering"]

        self.assertEqual(offering["backend_type"], "slurm")
        for field in config_builder.AGENT_ROLE_FIELDS:
            self.assertEqual(offering[field], "slurm")
        self.assertEqual(
            offering["backend_settings"],
            {
                "default_account": "root",
                "customer_prefix": "",
                "project_prefix": "",
                "allocation_prefix": "",
                "qos_downscaled": "limited",
                "qos_paused": "paused",
                "qos_default": "normal",
                "enable_user_homedir_account_creation": False,
            },
        )

    def test_legacy_offering_keeps_a_hostname_it_sets(self):
        offering = self._config()["offering"]

        self.assertEqual(
            offering["backend_settings"]["hostname"], "slurm.test.example.com"
        )
