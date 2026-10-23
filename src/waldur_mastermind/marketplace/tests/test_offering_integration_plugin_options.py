from ddt import data, ddt, unpack
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import serializers
from waldur_mastermind.marketplace.tests import factories


@ddt
class LifecyclePluginOptionsPersistenceTest(test.APITestCase):
    """Regression test: these lifecycle plugin options are edited by the Homeport
    offering-update UI but were missing from ``LifecyclePluginOptionsSerializer``,
    so the strict nested serializer silently dropped them from ``update_integration``
    and the values never persisted (the UI toggle snapped back to its old state)."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)

    def _update(self, plugin_options):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        response = self.client.post(url, {"plugin_options": plugin_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        return self.offering.plugin_options

    @data(
        ("enable_resource_access_subnets", True),
        ("conceal_subnet_restricted_resources", True),
        ("resource_projects_limit_policy", "per_project"),
        ("enable_resource_limit_change_requests", True),
        ("enable_resource_limit_change_requests", False),
        ("enable_scim_entitlements", True),
        ("enable_scim_entitlements", False),
    )
    @unpack
    def test_option_persists(self, key, value):
        self.assertEqual(self._update({key: value})[key], value)

    def test_declared_on_merged_serializer(self):
        serializer = serializers.MergedPluginOptionsSerializer(
            data={
                "enable_resource_access_subnets": True,
                "conceal_subnet_restricted_resources": True,
                "resource_projects_limit_policy": "aggregate",
                "enable_resource_limit_change_requests": True,
                "enable_scim_entitlements": True,
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["enable_resource_limit_change_requests"], True
        )
        self.assertEqual(
            serializer.validated_data["enable_resource_access_subnets"], True
        )
        self.assertEqual(
            serializer.validated_data["conceal_subnet_restricted_resources"], True
        )
        self.assertEqual(
            serializer.validated_data["resource_projects_limit_policy"], "aggregate"
        )
        self.assertEqual(serializer.validated_data["enable_scim_entitlements"], True)

    def test_scim_login_nodes_persist_as_bare_hostnames(self):
        result = self._update(
            {
                "scim_login_nodes": [
                    "Login.Example.ORG.",
                    "login.example.org",
                    "  other.example.org  ",
                ]
            }
        )
        self.assertEqual(
            result["scim_login_nodes"],
            ["login.example.org", "other.example.org"],
        )

    @data(
        "ssh://login.example.org",
        "user@login.example.org",
        "login.example.org:22",
        "not a hostname",
    )
    def test_scim_login_nodes_reject_non_hostnames(self, hostname):
        serializer = serializers.MergedPluginOptionsSerializer(
            data={"scim_login_nodes": [hostname]}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("scim_login_nodes", serializer.errors)

    def test_invalid_limit_policy_rejected(self):
        serializer = serializers.MergedPluginOptionsSerializer(
            data={"resource_projects_limit_policy": "bogus"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("resource_projects_limit_policy", serializer.errors)

    def test_options_preserved_alongside_existing(self):
        self.offering.plugin_options = {"auto_approve_remote_orders": True}
        self.offering.save()
        result = self._update({"enable_resource_access_subnets": True})
        self.assertEqual(result["enable_resource_access_subnets"], True)
        self.assertEqual(result["auto_approve_remote_orders"], True)

    def test_uses_robot_accounts_persists(self):
        self.assertIs(
            self._update({"uses_robot_accounts": True})["uses_robot_accounts"],
            True,
        )

    def test_uses_robot_accounts_rejected_with_offering_users(self):
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        response = self.client.post(
            url, {"plugin_options": {"uses_robot_accounts": True}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.offering.refresh_from_db()
        self.assertNotIn("uses_robot_accounts", self.offering.plugin_options)

    def test_uses_robot_accounts_allowed_when_offering_users_disabled(self):
        result = self._update(
            {
                "uses_robot_accounts": True,
                "service_provider_can_create_offering_user": False,
            }
        )
        self.assertIs(result["uses_robot_accounts"], True)
        self.assertIs(result["service_provider_can_create_offering_user"], False)


@ddt
class ResourceApiKeyPluginOptionsPersistenceTest(test.APITestCase):
    """The API key options are read from plugin_options by the backend and the
    portal; an undeclared key would be dropped by update_integration with a 200."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)

    @data(
        ("enable_api_key_provisioning", True),
        ("enable_api_key_provisioning", False),
        ("hide_api_keys_tab", True),
        ("hide_api_keys_tab", False),
        ("api_key_limits", True),
        ("api_key_limits", False),
        ("api_key_daily_usage", True),
        ("api_key_daily_usage", False),
    )
    @unpack
    def test_option_persists(self, key, value):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        options = {key: value}
        if key.startswith("api_key_"):
            options["enable_api_key_provisioning"] = True
        response = self.client.post(url, {"plugin_options": options}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.plugin_options[key], value)

    def test_another_option_update_keeps_the_capability(self):
        # The field has no default, so an update that leaves it out must not
        # reset it to False.
        self.offering.plugin_options = {"enable_api_key_provisioning": True}
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        response = self.client.post(
            url, {"plugin_options": {"hide_api_keys_tab": True}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertTrue(self.offering.plugin_options["enable_api_key_provisioning"])

    def _post(self, plugin_options):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        response = self.client.post(
            url, {"plugin_options": plugin_options}, format="json"
        )
        self.offering.refresh_from_db()
        return response

    def test_an_update_leaving_the_key_options_out_keeps_them(self):
        self.offering.plugin_options = {
            "enable_api_key_provisioning": True,
            "api_key_limits": False,
            "api_key_daily_usage": True,
        }
        self.offering.save()
        response = self._post({"hide_api_keys_tab": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIs(self.offering.plugin_options["api_key_limits"], False)
        self.assertIs(self.offering.plugin_options["api_key_daily_usage"], True)

    def test_turning_a_key_option_on_needs_key_management(self):
        for options in (
            {"api_key_daily_usage": True},
            {"api_key_daily_usage": True, "enable_api_key_provisioning": False},
        ):
            with self.subTest(options):
                response = self._post(options)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("enable_api_key_provisioning", str(response.data))
                self.assertNotIn("api_key_daily_usage", self.offering.plugin_options)
        self.offering.plugin_options = {"api_key_limits": False}
        self.offering.save()
        response = self._post({"api_key_limits": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_turning_key_options_off_or_leaving_them_as_they_are_is_allowed(self):
        self.offering.plugin_options = {
            "enable_api_key_provisioning": True,
            "api_key_daily_usage": True,
        }
        self.offering.save()
        # Key management off on its own, sending the rest as the form holds it.
        response = self._post(
            {
                "enable_api_key_provisioning": False,
                "api_key_limits": True,
                "api_key_daily_usage": True,
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self._post({"api_key_daily_usage": False, "api_key_limits": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
