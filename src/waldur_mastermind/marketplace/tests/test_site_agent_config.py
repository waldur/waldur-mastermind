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


@ddt
class SiteAgentConfigGenerationTest(test.APITransactionTestCase):
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

    def test_generate_config_hostname_placeholder_when_not_set(self):
        """Test that hostname shows placeholder when not configured."""
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
            config["offerings"][0]["backend_settings"]["hostname"],
            "<YOUR_SLURM_HOST>",
        )


class SiteAgentConfigWithPolicyTest(test.APITransactionTestCase):
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
