import string

import yaml
from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
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

        self.assertNotIn("BACKEND SECRETS", content)
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
        self.assertIn("bind_password: ''  # SECRET: ldap", content)
        self.assertIn("backend_settings.ldap.bind_password (ldap", content)
        self.assertNotIn("ldap.bind_password (ldap, optional)", content)

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
            {"waldur_api_token", "backend_settings.cscs_dwdi_client_secret"},
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
