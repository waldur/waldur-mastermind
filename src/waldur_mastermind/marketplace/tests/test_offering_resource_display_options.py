from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import serializers
from waldur_mastermind.marketplace.tests import factories


class OfferingResourceDisplayOptionsSerializerTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_serializer_default_values(self):
        """Test that the serializer provides correct default values"""
        serializer = serializers.OfferingResourceDisplayOptionsSerializer(data={})

        self.assertTrue(serializer.is_valid())

        # Test default values
        validated_data = serializer.validated_data
        self.assertEqual(
            validated_data.get("highlight_backend_id_display", False), False
        )
        self.assertEqual(
            validated_data.get("backend_id_display_label", "Backend ID"), "Backend ID"
        )
        self.assertEqual(
            validated_data.get("expose_inference_playground", False), False
        )

    def test_serializer_custom_values(self):
        """Test that the serializer accepts custom values"""
        data = {
            "highlight_backend_id_display": True,
            "backend_id_display_label": "Custom Backend Identifier",
            "expose_inference_playground": True,
        }

        serializer = serializers.OfferingResourceDisplayOptionsSerializer(data=data)

        self.assertTrue(serializer.is_valid())

        validated_data = serializer.validated_data
        self.assertEqual(validated_data["highlight_backend_id_display"], True)
        self.assertEqual(
            validated_data["backend_id_display_label"], "Custom Backend Identifier"
        )
        self.assertEqual(validated_data["expose_inference_playground"], True)

    def test_serializer_boolean_validation(self):
        """Test that boolean field validates correctly"""
        # Test invalid boolean values
        invalid_data = {
            "highlight_backend_id_display": "not_a_boolean",
            "backend_id_display_label": "Valid Label",
        }

        serializer = serializers.OfferingResourceDisplayOptionsSerializer(
            data=invalid_data
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("highlight_backend_id_display", serializer.errors)

    def test_serializer_string_validation(self):
        """Test that string field validates correctly"""
        data = {
            "highlight_backend_id_display": True,
            "backend_id_display_label": "",  # Empty string should be valid
        }

        serializer = serializers.OfferingResourceDisplayOptionsSerializer(data=data)

        self.assertTrue(serializer.is_valid())
        self.assertEqual(serializer.validated_data["backend_id_display_label"], "")

    def test_merged_plugin_options_serializer_includes_display_options(self):
        """Test that MergedPluginOptionsSerializer includes the new display options"""
        data = {
            "highlight_backend_id_display": True,
            "backend_id_display_label": "System ID",
            # Include other plugin options to ensure compatibility
            "auto_approve_remote_orders": False,
        }

        serializer = serializers.MergedPluginOptionsSerializer(data=data)

        self.assertTrue(serializer.is_valid())

        validated_data = serializer.validated_data
        self.assertEqual(validated_data["highlight_backend_id_display"], True)
        self.assertEqual(validated_data["backend_id_display_label"], "System ID")
        self.assertEqual(validated_data["auto_approve_remote_orders"], False)


class OfferingResourceDisplayOptionsIntegrationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_INTEGRATION
        )

        self.customer = self.fixture.customer
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)

    def test_update_offering_plugin_options_with_display_options(self):
        """Test that offering plugin options can be updated with display options"""
        self.client.force_authenticate(self.fixture.staff)

        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        plugin_options = {
            "highlight_backend_id_display": True,
            "backend_id_display_label": "Resource Identifier",
            "auto_approve_remote_orders": False,  # Include other options for completeness
        }

        response = self.client.post(url, {"plugin_options": plugin_options})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options["highlight_backend_id_display"], True
        )
        self.assertEqual(
            self.offering.plugin_options["backend_id_display_label"],
            "Resource Identifier",
        )
        self.assertEqual(
            self.offering.plugin_options["auto_approve_remote_orders"], False
        )

    def test_update_offering_plugin_options_with_default_display_values(self):
        """Test that default values are applied when not specified"""
        self.client.force_authenticate(self.fixture.staff)

        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        plugin_options = {
            "auto_approve_remote_orders": True,  # Only set non-display option
        }

        response = self.client.post(url, {"plugin_options": plugin_options})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        # Check that defaults are not overridden if not provided
        self.assertEqual(
            self.offering.plugin_options["auto_approve_remote_orders"], True
        )
        # Display options should not be set unless explicitly provided
        self.assertNotIn("highlight_backend_id_display", self.offering.plugin_options)
        self.assertNotIn("backend_id_display_label", self.offering.plugin_options)

    def test_update_offering_plugin_options_with_only_display_options(self):
        """Test updating only display options without affecting other options"""
        # First, set some initial plugin options
        self.offering.plugin_options = {
            "auto_approve_remote_orders": True,
            "service_provider_can_create_offering_user": False,
        }
        self.offering.save()

        self.client.force_authenticate(self.fixture.staff)

        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        plugin_options = {
            "highlight_backend_id_display": True,
            "backend_id_display_label": "Backend System ID",
        }

        response = self.client.post(url, {"plugin_options": plugin_options})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()

        # New display options should be set
        self.assertEqual(
            self.offering.plugin_options["highlight_backend_id_display"], True
        )
        self.assertEqual(
            self.offering.plugin_options["backend_id_display_label"],
            "Backend System ID",
        )

        # Existing options should be preserved
        self.assertEqual(
            self.offering.plugin_options["auto_approve_remote_orders"], True
        )
        self.assertEqual(
            self.offering.plugin_options["service_provider_can_create_offering_user"],
            False,
        )

    def test_offering_serialization_includes_display_options(self):
        """Test that offering serialization includes display options in plugin_options"""
        self.offering.plugin_options = {
            "highlight_backend_id_display": True,
            "backend_id_display_label": "Custom Label",
            "auto_approve_remote_orders": False,
        }
        self.offering.save()

        self.client.force_authenticate(self.fixture.staff)

        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        plugin_options = response.data["plugin_options"]
        self.assertEqual(plugin_options["highlight_backend_id_display"], True)
        self.assertEqual(plugin_options["backend_id_display_label"], "Custom Label")
        self.assertEqual(plugin_options["auto_approve_remote_orders"], False)

    def test_customer_owner_can_update_display_options(self):
        """Test that customer owner can update display options"""
        self.client.force_authenticate(self.fixture.owner)

        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        plugin_options = {
            "highlight_backend_id_display": True,
            "backend_id_display_label": "Owner Set Label",
        }

        response = self.client.post(url, {"plugin_options": plugin_options})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options["highlight_backend_id_display"], True
        )
        self.assertEqual(
            self.offering.plugin_options["backend_id_display_label"], "Owner Set Label"
        )

    def test_service_provider_manager_can_update_display_options(self):
        """Test that service provider manager can update display options"""
        self.client.force_authenticate(self.fixture.service_manager)

        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        plugin_options = {
            "highlight_backend_id_display": False,
            "backend_id_display_label": "Manager Set Label",
        }

        response = self.client.post(url, {"plugin_options": plugin_options})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options["highlight_backend_id_display"], False
        )
        self.assertEqual(
            self.offering.plugin_options["backend_id_display_label"],
            "Manager Set Label",
        )

    def test_staff_can_update_disabled_resource_actions(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        plugin_options = {
            "disabled_resource_actions": ["terminate"],
        }
        response = self.client.post(url, {"plugin_options": plugin_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options["disabled_resource_actions"], ["terminate"]
        )

    def test_owner_cannot_update_disabled_resource_actions(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        plugin_options = {
            "disabled_resource_actions": ["terminate"],
        }
        response = self.client.post(url, {"plugin_options": plugin_options})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("plugin_options", response.data)
        self.assertEqual(
            response.data["plugin_options"][0],
            "Only staff can change list of disabled actions.",
        )

    def test_owner_can_update_other_plugin_options_with_same_disabled_actions(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        # Set initial value for disabled_resource_actions
        self.offering.plugin_options = {"disabled_resource_actions": ["terminate"]}
        self.offering.save()

        plugin_options = {
            "disabled_resource_actions": ["terminate"],  # Same as before
            "auto_approve_remote_orders": True,
        }
        response = self.client.post(url, {"plugin_options": plugin_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options["auto_approve_remote_orders"], True
        )
        self.assertEqual(
            self.offering.plugin_options["disabled_resource_actions"], ["terminate"]
        )
