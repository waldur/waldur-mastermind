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
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["enable_resource_access_subnets"], True
        )
        self.assertEqual(
            serializer.validated_data["conceal_subnet_restricted_resources"], True
        )
        self.assertEqual(
            serializer.validated_data["resource_projects_limit_policy"], "aggregate"
        )

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
