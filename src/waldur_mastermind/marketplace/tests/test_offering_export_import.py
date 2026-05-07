import yaml
from ddt import data, ddt
from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class OfferingExportImportTestCase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.user = self.fixture.owner
        # Create a category for tests since MarketplaceFixture doesn't provide one
        self.category = factories.CategoryFactory(title="Test Category")
        # Update the fixture offering to use our category
        self.fixture.offering.category = self.category
        self.fixture.offering.save()
        # Ensure user has permission to manage the offering
        self.fixture.offering.customer = self.customer
        self.fixture.offering.save()
        # Add user as customer owner to have permission to create offerings
        self.customer.add_user(self.user, CustomerRole.OWNER)
        # Add CREATE_OFFERING permission to CustomerRole.OWNER for import tests
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        # Also add as offering manager to have export permissions
        self.fixture.offering.add_user(self.user, OfferingRole.MANAGER)

    def test_export_offering_with_all_components(self):
        """Test exporting offering with all connected components."""
        self.client.force_authenticate(self.user)

        # Create offering with all related components
        offering = self.fixture.offering

        # Use existing component from fixture
        component = offering.components.first()
        if component:
            # Update the existing component to have meaningful values for testing
            component.name = "CPU"
            component.billing_type = "usage"
            component.measured_unit = "hours"
            component.save()

        # Add plan
        plan = factories.PlanFactory(offering=offering)
        factories.PlanComponentFactory(plan=plan, component=component, price=10)

        # Add screenshot
        factories.ScreenshotFactory(offering=offering)

        # Add file
        factories.OfferingFileFactory(offering=offering)

        # Add endpoint manually since factory doesn't exist
        models.OfferingAccessEndpoint.objects.create(
            offering=offering, name="Test API", url="https://api.test.com"
        )

        url = factories.OfferingFactory.get_url(offering, "export_offering")
        response = self.client.post(
            url,
            {
                "include_components": True,
                "include_plans": True,
                "include_screenshots": True,
                "include_files": True,
                "include_endpoints": True,
                "include_attributes": True,
                "include_options": True,
                "include_resource_options": True,
                "include_plugin_options": True,
                "include_secret_options": False,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Get structured response data
        export_data = response.data["export_data"]

        # Verify offering data
        self.assertEqual(export_data["offering"]["name"], offering.name)
        self.assertEqual(export_data["offering"]["type"], offering.type)
        self.assertEqual(export_data["offering"]["state"], offering.get_state_display())
        self.assertEqual(
            export_data["offering"]["category_name"], offering.category.title
        )

        # Verify components (fixture creates multiple components)
        self.assertGreaterEqual(len(export_data["components"]), 1)
        # Find the CPU component we updated
        cpu_component = next(
            (c for c in export_data["components"] if c["type"] == component.type), None
        )
        self.assertIsNotNone(cpu_component)
        self.assertEqual(cpu_component["name"], component.name)

        # Verify plans (fixture may create additional plans)
        self.assertGreaterEqual(len(export_data["plans"]), 1)
        # Find the specific plan we created for this test
        test_plan = next(
            (p for p in export_data["plans"] if p["name"] == plan.name), None
        )
        self.assertIsNotNone(test_plan)
        self.assertGreaterEqual(len(test_plan["components"]), 1)

        # Verify other components are present
        self.assertIn("screenshots", export_data)
        self.assertIn("files", export_data)
        self.assertIn("endpoints", export_data)

        # Verify exported components list
        exported_components = response.data["exported_components"]
        self.assertIn(component.type, exported_components)  # Use actual component type
        self.assertIn("plans", exported_components)
        self.assertIn("screenshots", exported_components)
        self.assertIn("files", exported_components)
        self.assertIn("endpoints", exported_components)

    def test_export_offering_selective_components(self):
        """Test exporting offering with selective components."""
        self.client.force_authenticate(self.user)

        offering = self.fixture.offering
        # Use existing components from fixture
        component = offering.components.first()
        if not component:
            factories.OfferingComponentFactory(offering=offering)
        plan = offering.plans.first()
        if not plan:
            factories.PlanFactory(offering=offering)

        url = factories.OfferingFactory.get_url(offering, "export_offering")
        response = self.client.post(
            url,
            {
                "include_components": True,
                "include_plans": False,
                "include_screenshots": False,
                "include_files": False,
                "include_endpoints": False,
                "include_secret_options": False,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        export_data = response.data["export_data"]

        # Should have components but not plans
        self.assertIn("components", export_data)
        self.assertNotIn("plans", export_data)
        self.assertNotIn("screenshots", export_data)

        # Should not include secret options
        self.assertNotIn("secret_options", export_data)

    def test_export_offering_includes_secret_options(self):
        """Test exporting offering with secret options when explicitly requested."""
        self.client.force_authenticate(self.user)

        offering = self.fixture.offering
        offering.secret_options = {"api_key": "secret123"}
        offering.save()

        url = factories.OfferingFactory.get_url(offering, "export_offering")
        response = self.client.post(
            url,
            {
                "include_secret_options": True,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        export_data = response.data["export_data"]
        self.assertEqual(export_data["secret_options"], {"api_key": "secret123"})

    def test_export_offering_permission_denied_for_unauthorized_user(self):
        """Test that unauthorized users cannot export offerings."""
        unauthorized_user = structure_fixtures.UserFixture().user
        self.client.force_authenticate(unauthorized_user)

        url = factories.OfferingFactory.get_url(
            self.fixture.offering, "export_offering"
        )
        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_import_offering_creates_new_offering(self):
        """Test importing offering creates a new offering with all components."""
        self.client.force_authenticate(self.user)

        # Prepare import data
        import_data = {
            "offering": {
                "name": "Imported Test Offering",
                "description": "Description of imported offering",
                "type": "TEST_TYPE",
                "shared": True,
                "billable": True,
                "state": "Draft",
                "category_name": self.category.title,
                "attributes": {"key": "value"},
                "options": {"form": "data"},
                "resource_options": {"resource": "data"},
                "plugin_options": {"plugin": "data"},
            },
            "components": [
                {
                    "type": "cpu",
                    "name": "CPU",
                    "description": "CPU component",
                    "billing_type": "usage",
                    "measured_unit": "hours",
                    "unit_factor": 1.0,
                    "article_code": "CPU001",
                }
            ],
            "plans": [
                {
                    "name": "Basic Plan",
                    "description": "Basic plan description",
                    "unit_price": 10.0,
                    "unit": "month",
                    "components": [
                        {"component_type": "cpu", "amount": 2, "price": 5.0}
                    ],
                }
            ],
            "endpoints": [{"name": "API Endpoint", "url": "https://api.example.com"}],
        }

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "import_components": True,
                "import_plans": True,
                "import_endpoints": True,
                "overwrite_existing": False,
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify offering was created
        offering = models.Offering.objects.get(name="Imported Test Offering")
        self.assertEqual(offering.description, "Description of imported offering")
        self.assertEqual(offering.type, "TEST_TYPE")
        self.assertEqual(offering.customer, self.customer)
        self.assertEqual(offering.category, self.category)
        self.assertEqual(offering.state, models.Offering.States.DRAFT)
        self.assertEqual(offering.attributes, {"key": "value"})

        # Verify components were created
        self.assertEqual(offering.components.count(), 1)
        component = offering.components.first()
        self.assertEqual(component.type, "cpu")
        self.assertEqual(component.name, "CPU")

        # Verify plans were created
        self.assertEqual(offering.plans.count(), 1)
        plan = offering.plans.first()
        self.assertEqual(plan.name, "Basic Plan")
        self.assertEqual(plan.components.count(), 1)

        # Verify endpoints were created
        self.assertEqual(offering.endpoints.count(), 1)
        endpoint = offering.endpoints.first()
        self.assertEqual(endpoint.name, "API Endpoint")

        # Verify response data
        self.assertEqual(
            response.data["imported_offering_name"], "Imported Test Offering"
        )
        self.assertIn("cpu", response.data["imported_components"])
        self.assertIn("plans", response.data["imported_components"])
        self.assertIn("endpoints", response.data["imported_components"])

    def test_import_offering_updates_existing_offering(self):
        """Test importing offering updates existing offering when overwrite is enabled."""
        self.client.force_authenticate(self.user)

        # Create existing offering
        existing_offering = factories.OfferingFactory(
            customer=self.customer,
            name="Existing Offering",
            description="Old description",
        )

        import_data = {
            "offering": {
                "name": "Existing Offering",
                "description": "Updated description",
                "type": "UPDATED_TYPE",
            }
        }

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "overwrite_existing": True,
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify offering was updated
        existing_offering.refresh_from_db()
        self.assertEqual(existing_offering.description, "Updated description")
        self.assertEqual(existing_offering.type, "UPDATED_TYPE")

    def test_import_offering_fails_when_existing_offering_and_no_overwrite(self):
        """Test import fails when offering exists and overwrite is disabled."""
        self.client.force_authenticate(self.user)

        # Create existing offering
        factories.OfferingFactory(customer=self.customer, name="Existing Offering")

        import_data = {
            "offering": {
                "name": "Existing Offering",
                "description": "Updated description",
            }
        }

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "overwrite_existing": False,
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already exists", str(response.data))

    def test_import_offering_always_creates_draft_state(self):
        """Test that all imported offerings are created in DRAFT state for security."""
        self.client.force_authenticate(self.user)

        import_data = {"offering": {"name": "State Test Offering", "state": "Active"}}

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        offering = models.Offering.objects.get(name="State Test Offering")
        # Always DRAFT state regardless of input data
        self.assertEqual(offering.state, models.Offering.States.DRAFT)

    def test_import_offering_terms_of_service_link_null_is_coerced_to_blank(self):
        """Handle case where `terms_of_service_link:` is null in YAML data."""
        self.client.force_authenticate(self.user)

        import_data = {
            "offering": {"name": "ToS Link Null Test"},
            "terms_of_service": [
                {
                    "terms_of_service": "# Sample Terms\n\nThis is the sample ...",
                    "terms_of_service_link": None,
                    "version": "1.0",
                    "is_active": True,
                    "requires_reconsent": False,
                    "grace_period_days": 60,
                }
            ],
        }

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "import_terms_of_service": True,
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        offering = models.Offering.objects.get(name="ToS Link Null Test")
        tos = offering.terms_of_service_configs.get()
        self.assertEqual(tos.terms_of_service_link, "")

    def test_import_offering_with_invalid_yaml(self):
        """Test import fails with invalid YAML data."""
        self.client.force_authenticate(self.user)

        invalid_yaml = "invalid: yaml: data: ["  # Invalid YAML

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "offering_data": invalid_yaml,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid YAML data", str(response.data))

    def test_import_offering_with_missing_category_fallback(self):
        """Test import handles missing category with fallback."""
        self.client.force_authenticate(self.user)

        # Create another category to use as fallback
        factories.CategoryFactory()

        import_data = {
            "offering": {
                "name": "Category Test Offering",
                "category_name": "Non-existent Category Name",  # Non-existent name
            }
        }

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                # Don't specify category, should fallback to first available
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Should have warning about category not found
        self.assertTrue(
            any(
                "Category with name" in warning and "not found" in warning
                for warning in response.data["warnings"]
            )
        )

    def test_import_offering_components_with_missing_components_in_plans(self):
        """Test import handles plan components referencing non-existent components."""
        self.client.force_authenticate(self.user)

        import_data = {
            "offering": {
                "name": "Component Warning Test",
            },
            "components": [{"type": "cpu", "name": "CPU"}],
            "plans": [
                {
                    "name": "Test Plan",
                    "components": [
                        {
                            "component_type": "memory",  # This component doesn't exist
                            "amount": 4,
                            "price": 10.0,
                        }
                    ],
                }
            ],
        }

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Should have warning about missing component
        self.assertTrue(
            any(
                "Component type 'memory' not found" in warning
                for warning in response.data["warnings"]
            )
        )

    def test_import_offering_permission_denied_for_unauthorized_user(self):
        """Test that unauthorized users cannot import offerings."""
        unauthorized_user = structure_fixtures.UserFixture().user
        self.client.force_authenticate(unauthorized_user)

        import_data = {"offering": {"name": "Test"}}
        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "offering_data": yaml_data,
            },
        )

        # Unauthorized users should be denied permission to create offerings
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("screenshots", "files")
    def test_import_offering_with_unimplemented_components(self, component_type):
        """Test import with components that are not fully implemented (screenshots, files)."""
        self.client.force_authenticate(self.user)

        import_data = {
            "offering": {"name": "Unimplemented Test"},
            component_type: [{"name": f"Test {component_type}"}],
        }

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                f"import_{component_type}": True,
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Should either succeed or have warnings about missing content
        # May have warnings about URL reference only or missing content
        if response.data["warnings"]:
            self.assertTrue(
                any(
                    "url reference only" in warning.lower()
                    or "without content" in warning.lower()
                    for warning in response.data["warnings"]
                )
            )

    def test_export_import_roundtrip(self):
        """Test that exported data can be successfully imported back."""
        self.client.force_authenticate(self.user)

        # Create offering with components
        offering = self.fixture.offering
        # Use existing components from fixture to avoid uniqueness constraint
        component = offering.components.first()
        if component:
            component.name = "CPU Core"
            component.save()
        plan = factories.PlanFactory(offering=offering, name="Standard Plan")
        factories.PlanComponentFactory(plan=plan, component=component)

        # Export the offering
        export_url = factories.OfferingFactory.get_url(offering, "export_offering")
        export_response = self.client.post(
            export_url,
            {
                "include_components": True,
                "include_plans": True,
                "include_secret_options": False,
            },
        )

        self.assertEqual(export_response.status_code, status.HTTP_200_OK)

        # Modify the exported data to create a new offering
        export_data = export_response.data["export_data"]
        export_data["offering"]["name"] = "Roundtrip Test Offering"
        modified_yaml = yaml.safe_dump(export_data)

        # Import the modified data
        import_url = reverse("marketplace-provider-offering-import-offering")
        import_response = self.client.post(
            import_url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "import_components": True,
                "import_plans": True,
                "offering_data": modified_yaml,
            },
        )

        self.assertEqual(import_response.status_code, status.HTTP_201_CREATED)

        # Verify the imported offering matches the original structure
        imported_offering = models.Offering.objects.get(name="Roundtrip Test Offering")
        self.assertGreaterEqual(imported_offering.components.count(), 1)
        self.assertGreaterEqual(imported_offering.plans.count(), 1)

        imported_component = imported_offering.components.first()
        self.assertEqual(imported_component.type, component.type)
        self.assertEqual(imported_component.name, "CPU Core")

        # Find the specific imported plan
        imported_plan = imported_offering.plans.filter(name="Standard Plan").first()
        self.assertIsNotNone(imported_plan)
        self.assertGreaterEqual(imported_plan.components.count(), 1)

    def test_import_offering_category_override(self):
        """Test that explicit category parameter overrides category from export data."""
        self.client.force_authenticate(self.user)

        # Create two categories
        export_category = self.category
        override_category = factories.CategoryFactory(title="Override Category")

        import_data = {
            "offering": {
                "name": "Category Override Test",
                "description": "Testing category override",
                "category_name": export_category.title,  # Category from export
            }
        }

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": override_category.title,  # Explicit category parameter
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify the offering was created with the override category, not the export category
        offering = models.Offering.objects.get(name="Category Override Test")
        self.assertEqual(offering.category, override_category)
        self.assertNotEqual(offering.category, export_category)

    def test_import_offering_uses_export_category_when_no_override(self):
        """Test that export category is used when no explicit category is provided."""
        self.client.force_authenticate(self.user)

        # Create a different category for export
        export_category = factories.CategoryFactory(title="Export Category")

        import_data = {
            "offering": {
                "name": "Export Category Test",
                "description": "Testing export category usage",
                "category_name": export_category.title,
            }
        }

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                # No explicit category provided - should use export category
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify the offering was created with the export category
        offering = models.Offering.objects.get(name="Export Category Test")
        self.assertEqual(offering.category, export_category)

    def test_export_offering_includes_base64_content(self):
        """Test that exported offerings include base64 encoded file content."""
        self.client.force_authenticate(self.user)

        offering = self.fixture.offering

        # Create a screenshot with actual image content
        factories.ScreenshotFactory(offering=offering)

        # Create a file with actual content
        factories.OfferingFileFactory(offering=offering)

        url = factories.OfferingFactory.get_url(offering, "export_offering")
        response = self.client.post(
            url,
            {
                "include_screenshots": True,
                "include_files": True,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        export_data = response.data["export_data"]

        # Check screenshots have base64 content or fallback URL
        if export_data.get("screenshots"):
            screenshot_data = export_data["screenshots"][0]
            # Should have either base64 content or fallback URL
            self.assertTrue(
                "image_content" in screenshot_data or "image_url" in screenshot_data
            )
            if "image_content" in screenshot_data:
                # Verify it's valid base64
                self.assertIsInstance(screenshot_data["image_content"], str)
                self.assertIn("content_type", screenshot_data)
                self.assertIn("image_filename", screenshot_data)

        # Check files have base64 content or fallback URL
        if export_data.get("files"):
            file_data = export_data["files"][0]
            # Should have either base64 content or fallback URL
            self.assertTrue("file_content" in file_data or "file_url" in file_data)
            if "file_content" in file_data:
                # Verify it's valid base64
                self.assertIsInstance(file_data["file_content"], str)
                self.assertIn("content_type", file_data)
                self.assertIn("filename", file_data)

    def test_import_offering_with_secret_options(self):
        """Test importing offering with secret_options when explicitly requested."""
        self.client.force_authenticate(self.user)

        # Prepare import data with secret_options at the top level (not inside offering)
        # This matches the export format where secret_options is a sibling of "offering"
        import_data = {
            "offering": {
                "name": "Secret Options Test Offering",
                "description": "Testing secret options import",
                "type": "TEST_TYPE",
            },
            "secret_options": {
                "create": "create_script_content",
                "update": "update_script_content",
                "environ": [{"name": "API_KEY", "value": "secret123"}],
            },
            "plugin_options": {"plugin_key": "plugin_value"},
            "resource_options": {"resource_key": "resource_value"},
        }

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "import_secret_options": True,
                "import_plugin_options": True,
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify offering was created with secret_options
        offering = models.Offering.objects.get(name="Secret Options Test Offering")
        self.assertEqual(
            offering.secret_options,
            {
                "create": "create_script_content",
                "update": "update_script_content",
                "environ": [{"name": "API_KEY", "value": "secret123"}],
            },
        )
        self.assertEqual(offering.plugin_options, {"plugin_key": "plugin_value"})
        self.assertEqual(offering.resource_options, {"resource_key": "resource_value"})

    def test_import_offering_secret_options_not_imported_by_default(self):
        """Test that secret_options are NOT imported by default (import_secret_options=False)."""
        self.client.force_authenticate(self.user)

        import_data = {
            "offering": {
                "name": "No Secret Options Test",
                "description": "Testing secret options not imported by default",
            },
            "secret_options": {"should": "not be imported"},
        }

        yaml_data = yaml.safe_dump(import_data)

        url = reverse("marketplace-provider-offering-import-offering")
        response = self.client.post(
            url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                # import_secret_options defaults to False
                "offering_data": yaml_data,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify offering was created WITHOUT secret_options
        offering = models.Offering.objects.get(name="No Secret Options Test")
        self.assertEqual(offering.secret_options, {})

    def test_export_import_roundtrip_with_secret_options(self):
        """Test that exported secret_options can be successfully imported back."""
        self.client.force_authenticate(self.user)

        # Create offering with secret_options
        offering = self.fixture.offering
        offering.secret_options = {
            "create": "create_script",
            "terminate": "terminate_script",
            "environ": [{"name": "SECRET_KEY", "value": "test123"}],
        }
        offering.plugin_options = {"config": "value"}
        offering.resource_options = {"resource": "config"}
        offering.save()

        # Export the offering with secret_options
        export_url = factories.OfferingFactory.get_url(offering, "export_offering")
        export_response = self.client.post(
            export_url,
            {
                "include_components": True,
                "include_secret_options": True,
                "include_plugin_options": True,
                "include_resource_options": True,
            },
        )

        self.assertEqual(export_response.status_code, status.HTTP_200_OK)

        # Verify export contains secret_options
        export_data = export_response.data["export_data"]
        self.assertEqual(export_data["secret_options"], offering.secret_options)
        self.assertEqual(export_data["plugin_options"], offering.plugin_options)
        self.assertEqual(export_data["resource_options"], offering.resource_options)

        # Modify the exported data to create a new offering
        export_data["offering"]["name"] = "Roundtrip Secret Options Test"
        modified_yaml = yaml.safe_dump(export_data)

        # Import the modified data with secret_options
        import_url = reverse("marketplace-provider-offering-import-offering")
        import_response = self.client.post(
            import_url,
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "import_secret_options": True,
                "import_plugin_options": True,
                "offering_data": modified_yaml,
            },
        )

        self.assertEqual(import_response.status_code, status.HTTP_201_CREATED)

        # Verify the imported offering has the correct secret_options
        imported_offering = models.Offering.objects.get(
            name="Roundtrip Secret Options Test"
        )
        self.assertEqual(
            imported_offering.secret_options,
            {
                "create": "create_script",
                "terminate": "terminate_script",
                "environ": [{"name": "SECRET_KEY", "value": "test123"}],
            },
        )
        self.assertEqual(imported_offering.plugin_options, {"config": "value"})
        self.assertEqual(imported_offering.resource_options, {"resource": "config"})
