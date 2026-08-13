import json
import os
import shutil
import tempfile
from io import StringIO

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from waldur_core.permissions.models import Role, RolePermission, UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.policy.tests import factories as policy_factories


class ExportStructureCommandTest(TestCase):
    """Test suite for export_structure management command."""

    def setUp(self):
        """Set up test fixtures and create temporary directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.output_file_path = os.path.join(self.temp_dir, "test_export.json")

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _call_export_command(self, output_path=None, **kwargs):
        """Helper to call export_structure command and return output."""
        if output_path is None:
            output_path = self.output_file_path

        output = StringIO()
        call_command("export_structure", "-o", output_path, stdout=output, **kwargs)
        return output.getvalue()

    def _load_exported_json(self, file_path=None):
        """Helper to load and return exported JSON data."""
        if file_path is None:
            file_path = self.output_file_path

        with open(file_path, encoding="utf-8") as f:
            return json.load(f)

    # Basic Export Tests

    def test_export_users_with_all_fields(self):
        """Test that export captures all user fields correctly."""
        user = structure_factories.UserFactory(
            username="testuser",
            email="test@example.com",
            full_name="Test User",
            native_name="Test Native",
            phone_number="+123456789",
            organization="Test Org",
            job_title="Developer",
            civil_number="12345",
            is_staff=True,
            is_support=False,
            is_active=True,
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify users exported
        self.assertIn("users", data)
        self.assertEqual(len(data["users"]), 1)

        exported_user = data["users"][0]
        self.assertEqual(exported_user["uuid"], user.uuid.hex)
        self.assertEqual(exported_user["username"], "testuser")
        self.assertEqual(exported_user["email"], "test@example.com")
        self.assertEqual(exported_user["full_name"], "Test User")
        self.assertEqual(exported_user["native_name"], "Test Native")
        self.assertEqual(exported_user["phone_number"], "+123456789")
        self.assertEqual(exported_user["organization"], "Test Org")
        self.assertEqual(exported_user["job_title"], "Developer")
        self.assertEqual(exported_user["civil_number"], "12345")
        self.assertTrue(exported_user["is_staff"])
        self.assertFalse(exported_user["is_support"])
        self.assertTrue(exported_user["is_active"])
        self.assertIsNotNone(exported_user["date_joined"])

    def test_export_users_with_token_lifetime(self):
        """Test that export captures token_lifetime field correctly."""
        # Test with explicit token_lifetime value
        structure_factories.UserFactory(
            username="user_with_lifetime",
            email="lifetime@example.com",
            token_lifetime=7200,  # 2 hours
        )

        # Test with token_lifetime = None (unlimited)
        user_unlimited = structure_factories.UserFactory(
            username="user_unlimited",
            email="unlimited@example.com",
        )
        user_unlimited.token_lifetime = None
        user_unlimited.save()

        self._call_export_command()
        data = self._load_exported_json()

        # Find the exported users
        exported_users = {u["username"]: u for u in data["users"]}

        # Verify user with explicit token_lifetime
        self.assertIn("user_with_lifetime", exported_users)
        self.assertEqual(
            exported_users["user_with_lifetime"]["token_lifetime"],
            7200,
        )

        # Verify user with unlimited token (token_lifetime = None → exported as -1)
        self.assertIn("user_unlimited", exported_users)
        self.assertEqual(
            exported_users["user_unlimited"]["token_lifetime"],
            -1,
            "token_lifetime should be -1 for unlimited tokens",
        )

    def test_export_customers_with_all_fields(self):
        """Test that export captures all customer fields correctly."""
        customer = structure_factories.CustomerFactory(
            name="Test Customer",
            native_name="Native Customer",
            abbreviation="TC",
            email="customer@example.com",
            phone_number="+987654321",
            country="EE",
            vat_code="VAT123",
            vat_name="VAT Name",
            vat_address="VAT Address",
            contact_details="Contact details",
            agreement_number="AGR123",
            registration_code="REG123",
            homepage="https://example.com",
            domain="example.com",
            address="Test Address",
            postal="12345",
            blocked=False,
            archived=False,
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify customers exported
        self.assertIn("customers", data)
        self.assertEqual(len(data["customers"]), 1)

        exported_customer = data["customers"][0]
        self.assertEqual(exported_customer["uuid"], customer.uuid.hex)
        self.assertEqual(exported_customer["name"], "Test Customer")
        self.assertEqual(exported_customer["native_name"], "Native Customer")
        self.assertEqual(exported_customer["abbreviation"], "TC")
        self.assertEqual(exported_customer["email"], "customer@example.com")
        self.assertEqual(exported_customer["phone_number"], "+987654321")
        self.assertEqual(exported_customer["country"], "EE")
        self.assertEqual(exported_customer["vat_code"], "VAT123")
        self.assertEqual(exported_customer["registration_code"], "REG123")
        self.assertFalse(exported_customer["blocked"])
        self.assertFalse(exported_customer["archived"])

    def test_export_projects_with_customer_relationships(self):
        """Test that export captures project data with customer FK."""
        customer = structure_factories.CustomerFactory(name="Project Customer")
        project = structure_factories.ProjectFactory(
            name="Test Project",
            description="Test project description",
            customer=customer,
            oecd_fos_2007_code="1.1",
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify projects exported
        self.assertIn("projects", data)
        self.assertEqual(len(data["projects"]), 1)

        exported_project = data["projects"][0]
        self.assertEqual(exported_project["uuid"], project.uuid.hex)
        self.assertEqual(exported_project["name"], "Test Project")
        self.assertEqual(exported_project["description"], "Test project description")
        self.assertEqual(exported_project["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_project["customer_name"], "Project Customer")
        self.assertEqual(exported_project["oecd_fos_2007_code"], "1.1")
        self.assertIsNotNone(exported_project["created"])

    def test_export_categories(self):
        """Test that export captures marketplace categories."""
        category = marketplace_factories.CategoryFactory(
            title="Test Category",
            description="Test description",
            backend_id="test_backend",
            default_vm_category=True,
            default_volume_category=False,
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify categories exported
        self.assertIn("categories", data)
        self.assertEqual(len(data["categories"]), 1)

        exported_category = data["categories"][0]
        self.assertEqual(exported_category["uuid"], category.uuid.hex)
        self.assertEqual(exported_category["title"], "Test Category")
        self.assertEqual(exported_category["description"], "Test description")
        self.assertEqual(exported_category["backend_id"], "test_backend")
        self.assertTrue(exported_category["default_vm_category"])
        self.assertFalse(exported_category["default_volume_category"])

    def test_export_offerings_with_relationships(self):
        """Test that export captures offerings with FK relationships."""
        customer = structure_factories.CustomerFactory(name="Offering Customer")
        category = marketplace_factories.CategoryFactory(title="Offering Category")
        offering = marketplace_factories.OfferingFactory(
            name="Test Offering",
            description="Test offering description",
            type="Test.Type",
            customer=customer,
            category=category,
            shared=True,
            billable=False,
            attributes={"key": "value"},
            options={"option": "value"},
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify offerings exported
        self.assertIn("offerings", data)
        self.assertEqual(len(data["offerings"]), 1)

        exported_offering = data["offerings"][0]
        self.assertEqual(exported_offering["uuid"], offering.uuid.hex)
        self.assertEqual(exported_offering["name"], "Test Offering")
        self.assertEqual(exported_offering["description"], "Test offering description")
        self.assertEqual(exported_offering["type"], "Test.Type")
        self.assertEqual(exported_offering["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_offering["customer_name"], "Offering Customer")
        self.assertEqual(exported_offering["category_uuid"], category.uuid.hex)
        self.assertEqual(exported_offering["category_title"], "Offering Category")
        self.assertTrue(exported_offering["shared"])
        self.assertFalse(exported_offering["billable"])
        self.assertEqual(exported_offering["attributes"], {"key": "value"})
        self.assertEqual(exported_offering["options"], {"option": "value"})

    def test_export_roles_with_content_types(self):
        """Test that export captures roles with content type information."""
        from waldur_core.structure.models import Customer

        content_type = ContentType.objects.get_for_model(Customer)
        role = Role.objects.create(
            name="TestRole",
            description="Test role description",
            content_type=content_type,
            is_system_role=True,
            is_active=True,
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify roles exported
        self.assertIn("roles", data)
        exported_roles = [r for r in data["roles"] if r["name"] == "TestRole"]
        self.assertEqual(len(exported_roles), 1)

        exported_role = exported_roles[0]
        self.assertEqual(exported_role["uuid"], role.uuid.hex)
        self.assertEqual(exported_role["name"], "TestRole")
        self.assertEqual(exported_role["description"], "Test role description")
        self.assertEqual(
            exported_role["content_type"],
            f"{content_type.app_label}.{content_type.model}",
        )
        self.assertTrue(exported_role["is_system_role"])
        self.assertTrue(exported_role["is_active"])

    def test_export_user_roles_with_scopes(self):
        """Test that export captures user roles with generic FK scopes."""
        user = structure_factories.UserFactory()
        customer = structure_factories.CustomerFactory()
        content_type = ContentType.objects.get_for_model(customer)
        role = Role.objects.create(
            name="TestRole",
            description="Test",
            content_type=content_type,
            is_active=True,
        )

        user_role = UserRole.objects.create(
            user=user,
            role=role,
            content_type=content_type,
            object_id=customer.id,
            is_active=True,
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify user roles exported
        self.assertIn("user_roles", data)
        exported_user_roles = [
            ur for ur in data["user_roles"] if ur["uuid"] == user_role.uuid.hex
        ]
        self.assertEqual(len(exported_user_roles), 1)

        exported_user_role = exported_user_roles[0]
        self.assertEqual(exported_user_role["user_uuid"], user.uuid.hex)
        self.assertEqual(exported_user_role["user_username"], user.username)
        self.assertEqual(exported_user_role["role_uuid"], role.uuid.hex)
        self.assertEqual(exported_user_role["role_name"], "TestRole")
        self.assertEqual(
            exported_user_role["scope_type"],
            f"{content_type.app_label}.{content_type.model}",
        )
        self.assertEqual(exported_user_role["scope_uuid"], str(customer.uuid.hex))
        self.assertEqual(exported_user_role["scope_name"], customer.name)
        self.assertTrue(exported_user_role["is_active"])

    def test_export_role_permissions(self):
        """Test that export captures role permission mappings."""
        from waldur_core.structure.models import Customer

        content_type = ContentType.objects.get_for_model(Customer)
        role = Role.objects.create(
            name="TestRole",
            description="Test",
            content_type=content_type,
            is_active=True,
        )

        RolePermission.objects.create(role=role, permission="CUSTOMER.VIEW")
        RolePermission.objects.create(role=role, permission="CUSTOMER.UPDATE")

        self._call_export_command()
        data = self._load_exported_json()

        # Verify role permissions exported
        self.assertIn("role_permissions", data)
        exported_perms = [
            rp for rp in data["role_permissions"] if rp["role_name"] == "TestRole"
        ]
        self.assertEqual(len(exported_perms), 2)

        permissions = [rp["permission"] for rp in exported_perms]
        self.assertIn("CUSTOMER.VIEW", permissions)
        self.assertIn("CUSTOMER.UPDATE", permissions)

    # File Handling Tests

    def test_successful_file_creation(self):
        """Test that export creates file at specified path."""
        self._call_export_command()

        # Verify file exists
        self.assertTrue(os.path.exists(self.output_file_path))

        # Verify it's valid JSON
        data = self._load_exported_json()
        self.assertIsInstance(data, dict)

    def test_output_directory_validation(self):
        """Test that non-existent output directory shows error."""
        invalid_path = "/nonexistent/directory/output.json"

        output = self._call_export_command(invalid_path)

        # Verify error message
        self.assertIn("Output directory does not exist", output)
        self.assertNotIn("Successfully exported", output)

    def test_file_write_error_handling(self):
        """Test error handling for file write failures."""
        # Try to write to a directory path instead of file
        invalid_path = self.temp_dir  # This is a directory, not a file

        output = self._call_export_command(invalid_path)

        # Verify error message (will fail because writing to directory)
        self.assertIn("Failed to write to file", output)

    def test_json_format_validity(self):
        """Test that generated JSON is valid and well-formatted."""
        structure_factories.UserFactory()
        structure_factories.CustomerFactory()

        self._call_export_command()

        # Load JSON and verify it's valid
        data = self._load_exported_json()

        # Verify it's a dict with expected keys
        self.assertIsInstance(data, dict)
        expected_keys = [
            "users",
            "customers",
            "projects",
            "categories",
            "offerings",
            "roles",
            "user_roles",
            "role_permissions",
            "project_service_accounts",
            "customer_service_accounts",
            "course_accounts",
            "resources",
            "offering_components",
            "component_usages",
            "plans",
            "plan_components",
            "invoices",
            "invoice_items",
        ]
        for key in expected_keys:
            self.assertIn(key, data)
            self.assertIsInstance(data[key], list)

    # Data Validation Tests

    def test_json_structure_has_all_expected_keys(self):
        """Test that exported JSON has all expected top-level keys."""
        self._call_export_command()
        data = self._load_exported_json()

        expected_keys = [
            "users",
            "customers",
            "projects",
            "categories",
            "offerings",
            "roles",
            "user_roles",
            "role_permissions",
            "project_service_accounts",
            "customer_service_accounts",
            "course_accounts",
            "resources",
            "offering_components",
            "component_usages",
            "plans",
            "plan_components",
            "invoices",
            "invoice_items",
        ]

        for key in expected_keys:
            self.assertIn(key, data, f"Missing expected key: {key}")

    def test_uuids_are_strings(self):
        """Test that UUIDs are serialized as strings, not UUID objects."""
        user = structure_factories.UserFactory()
        customer = structure_factories.CustomerFactory()

        self._call_export_command()
        data = self._load_exported_json()

        # Verify UUIDs are strings
        self.assertIsInstance(data["users"][0]["uuid"], str)
        self.assertIsInstance(data["customers"][0]["uuid"], str)
        self.assertEqual(data["users"][0]["uuid"], user.uuid.hex)
        self.assertEqual(data["customers"][0]["uuid"], customer.uuid.hex)

    def test_uuids_are_hex_format_without_hyphens(self):
        """Test that UUIDs use hex format (without hyphens)."""
        structure_factories.UserFactory()

        self._call_export_command()
        data = self._load_exported_json()

        user_uuid = data["users"][0]["uuid"]
        # Hex format should not contain hyphens
        self.assertNotIn("-", user_uuid)
        # Hex format should be 32 characters (without hyphens)
        self.assertEqual(len(user_uuid), 32)
        # Should be valid hex string
        int(user_uuid, 16)  # Will raise ValueError if not valid hex

    def test_datetime_fields_are_iso_formatted(self):
        """Test that datetime fields are ISO formatted strings."""
        structure_factories.UserFactory()

        self._call_export_command()
        data = self._load_exported_json()

        # Verify date_joined is a string in ISO format
        date_joined = data["users"][0]["date_joined"]
        self.assertIsInstance(date_joined, str)
        # Should be able to parse as ISO format (contains 'T' separator)
        self.assertIn("T", date_joined)

    def test_empty_database_exports_correctly(self):
        """Test that export works with empty database."""
        # Delete all data
        from waldur_core.core.models import User
        from waldur_core.structure.models import Customer

        User.objects.all().delete()
        Customer.objects.all().delete()

        self._call_export_command()
        data = self._load_exported_json()

        # Verify all lists are empty
        self.assertEqual(len(data["users"]), 0)
        self.assertEqual(len(data["customers"]), 0)
        self.assertEqual(len(data["projects"]), 0)

    # Summary Statistics Tests

    def test_summary_output_shows_correct_counts(self):
        """Test that summary statistics show correct counts."""
        structure_factories.UserFactory()
        structure_factories.UserFactory()
        structure_factories.CustomerFactory()

        output = self._call_export_command()

        # Verify summary includes counts
        self.assertIn("Export summary:", output)
        self.assertIn("Users: 2", output)
        self.assertIn("Customers: 1", output)

    def test_success_message_printed(self):
        """Test that success message is printed after export."""
        output = self._call_export_command()

        # Verify success message
        self.assertIn("Successfully exported structure data", output)
        self.assertIn(self.output_file_path, output)

    def test_export_project_service_accounts(self):
        """Test that export captures project service accounts with all fields."""
        customer = structure_factories.CustomerFactory(name="Account Customer")
        project = structure_factories.ProjectFactory(
            name="Account Project", customer=customer
        )
        account = marketplace_factories.ProjectServiceAccountFactory(
            project=project,
            username="test_service_account",
            email="service@example.com",
            preferred_identifier="test_pref_id",
            description="Test service account description",
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify project service accounts exported
        self.assertIn("project_service_accounts", data)
        self.assertEqual(len(data["project_service_accounts"]), 1)

        exported_account = data["project_service_accounts"][0]
        self.assertEqual(exported_account["uuid"], account.uuid.hex)
        self.assertEqual(exported_account["username"], "test_service_account")
        self.assertEqual(exported_account["email"], "service@example.com")
        self.assertEqual(exported_account["preferred_identifier"], "test_pref_id")
        self.assertEqual(
            exported_account["description"], "Test service account description"
        )
        self.assertEqual(exported_account["state"], account.state)
        self.assertEqual(exported_account["project_uuid"], project.uuid.hex)
        self.assertEqual(exported_account["project_name"], "Account Project")
        self.assertEqual(exported_account["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_account["customer_name"], "Account Customer")
        self.assertIsNotNone(exported_account["created"])

    def test_export_customer_service_accounts(self):
        """Test that export captures customer service accounts with all fields."""
        customer = structure_factories.CustomerFactory(name="Service Customer")
        account = marketplace_factories.CustomerServiceAccountFactory(
            customer=customer,
            username="customer_service_account",
            email="customer_service@example.com",
            preferred_identifier="customer_pref_id",
            description="Customer service account description",
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify customer service accounts exported
        self.assertIn("customer_service_accounts", data)
        self.assertEqual(len(data["customer_service_accounts"]), 1)

        exported_account = data["customer_service_accounts"][0]
        self.assertEqual(exported_account["uuid"], account.uuid.hex)
        self.assertEqual(exported_account["username"], "customer_service_account")
        self.assertEqual(exported_account["email"], "customer_service@example.com")
        self.assertEqual(exported_account["preferred_identifier"], "customer_pref_id")
        self.assertEqual(
            exported_account["description"], "Customer service account description"
        )
        self.assertEqual(exported_account["state"], account.state)
        self.assertEqual(exported_account["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_account["customer_name"], "Service Customer")
        self.assertIsNotNone(exported_account["created"])

    def test_export_course_accounts(self):
        """Test that export captures course accounts with all fields."""
        user = structure_factories.UserFactory(username="course_user")
        customer = structure_factories.CustomerFactory(name="Course Customer")
        project = structure_factories.ProjectFactory(
            name="Course Project", customer=customer
        )
        account = marketplace_factories.CourseAccountFactory(
            project=project,
            user=user,
            email="course@example.com",
            description="Course account description",
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify course accounts exported
        self.assertIn("course_accounts", data)
        self.assertEqual(len(data["course_accounts"]), 1)

        exported_account = data["course_accounts"][0]
        self.assertEqual(exported_account["uuid"], account.uuid.hex)
        self.assertEqual(exported_account["email"], "course@example.com")
        self.assertEqual(exported_account["description"], "Course account description")
        self.assertEqual(exported_account["state"], account.state)
        self.assertEqual(exported_account["user_uuid"], user.uuid.hex)
        self.assertEqual(exported_account["user_username"], "course_user")
        self.assertEqual(exported_account["project_uuid"], project.uuid.hex)
        self.assertEqual(exported_account["project_name"], "Course Project")
        self.assertEqual(exported_account["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_account["customer_name"], "Course Customer")
        self.assertEqual(exported_account["error_message"], "")
        self.assertIsNotNone(exported_account["created"])

    def test_export_resources(self):
        """Test that export captures resources with all relationships."""
        customer = structure_factories.CustomerFactory(name="Resource Customer")
        project = structure_factories.ProjectFactory(
            name="Resource Project", customer=customer
        )
        offering = marketplace_factories.OfferingFactory(name="Test Offering")
        plan = marketplace_factories.PlanFactory(
            offering=offering, name="Test Plan", unit_price=100
        )
        resource = marketplace_factories.ResourceFactory(
            name="Test Resource",
            offering=offering,
            plan=plan,
            project=project,
            attributes={"key": "value"},
            limits={"cpu": 2},
            options={"option": "value"},
            backend_id="backend-123",
            effective_id="effective-456",
            description="Test resource description",
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("resources", data)
        self.assertEqual(len(data["resources"]), 1)

        exported_resource = data["resources"][0]
        self.assertEqual(exported_resource["uuid"], resource.uuid.hex)
        self.assertEqual(exported_resource["name"], "Test Resource")
        self.assertEqual(exported_resource["state"], resource.state)
        self.assertEqual(exported_resource["offering_uuid"], offering.uuid.hex)
        self.assertEqual(exported_resource["offering_name"], "Test Offering")
        self.assertEqual(exported_resource["plan_uuid"], plan.uuid.hex)
        self.assertEqual(exported_resource["plan_name"], "Test Plan")
        self.assertEqual(exported_resource["project_uuid"], project.uuid.hex)
        self.assertEqual(exported_resource["project_name"], "Resource Project")
        self.assertEqual(exported_resource["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_resource["customer_name"], "Resource Customer")
        self.assertEqual(exported_resource["attributes"], {"key": "value"})
        self.assertEqual(exported_resource["limits"], {"cpu": 2})
        self.assertEqual(exported_resource["backend_id"], "backend-123")
        self.assertEqual(exported_resource["effective_id"], "effective-456")
        self.assertIsNotNone(exported_resource["created"])

    def test_export_offering_components(self):
        """Test that export captures offering components with all fields."""
        offering = marketplace_factories.OfferingFactory(name="Component Offering")
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type="cpu",
            name="CPU",
            description="CPU component",
            billing_type="fixed",
            measured_unit="cores",
            limit_period="month",
            limit_amount=10,
            article_code="CPU-001",
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("offering_components", data)
        self.assertEqual(len(data["offering_components"]), 1)

        exported_component = data["offering_components"][0]
        self.assertEqual(exported_component["uuid"], component.uuid.hex)
        self.assertEqual(exported_component["offering_uuid"], offering.uuid.hex)
        self.assertEqual(exported_component["offering_name"], "Component Offering")
        self.assertEqual(exported_component["type"], "cpu")
        self.assertEqual(exported_component["name"], "CPU")
        self.assertEqual(exported_component["description"], "CPU component")
        self.assertEqual(exported_component["billing_type"], "fixed")
        self.assertEqual(exported_component["measured_unit"], "cores")
        self.assertEqual(exported_component["limit_period"], "month")
        self.assertEqual(exported_component["limit_amount"], 10)
        self.assertEqual(exported_component["article_code"], "CPU-001")

    def test_export_component_usages(self):
        """Test that export captures component usages with all fields."""
        resource = marketplace_factories.ResourceFactory(name="Usage Resource")
        component = marketplace_factories.OfferingComponentFactory(
            offering=resource.offering, type="storage", name="Storage"
        )
        usage = marketplace_factories.ComponentUsageFactory(
            resource=resource,
            component=component,
            usage=100.5,
            recurring=True,
            description="Monthly storage usage",
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("component_usages", data)
        self.assertEqual(len(data["component_usages"]), 1)

        exported_usage = data["component_usages"][0]
        self.assertEqual(exported_usage["uuid"], usage.uuid.hex)
        self.assertEqual(exported_usage["resource_uuid"], resource.uuid.hex)
        self.assertEqual(exported_usage["resource_name"], "Usage Resource")
        self.assertEqual(exported_usage["component_uuid"], component.uuid.hex)
        self.assertEqual(exported_usage["component_type"], "storage")
        self.assertEqual(exported_usage["component_name"], "Storage")
        self.assertEqual(exported_usage["usage"], "100.50")
        self.assertTrue(exported_usage["recurring"])
        self.assertEqual(exported_usage["description"], "Monthly storage usage")
        self.assertIsNotNone(exported_usage["date"])
        self.assertIsNotNone(exported_usage["billing_period"])

    def test_export_plans(self):
        """Test that export captures plans with all fields."""
        offering = marketplace_factories.OfferingFactory(name="Plan Offering")
        plan = marketplace_factories.PlanFactory(
            offering=offering,
            name="Premium Plan",
            description="Premium plan description",
            unit_price=250.5,
            unit="month",
            archived=False,
            max_amount=5,
            article_code="PLAN-PREMIUM",
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("plans", data)
        exported_plans = [p for p in data["plans"] if p["uuid"] == plan.uuid.hex]
        self.assertEqual(len(exported_plans), 1)

        exported_plan = exported_plans[0]
        self.assertEqual(exported_plan["uuid"], plan.uuid.hex)
        self.assertEqual(exported_plan["offering_uuid"], offering.uuid.hex)
        self.assertEqual(exported_plan["offering_name"], "Plan Offering")
        self.assertEqual(exported_plan["name"], "Premium Plan")
        self.assertEqual(exported_plan["description"], "Premium plan description")
        self.assertEqual(float(exported_plan["unit_price"]), 250.5)
        self.assertEqual(exported_plan["unit"], "month")
        self.assertFalse(exported_plan["archived"])
        self.assertEqual(exported_plan["max_amount"], 5)
        self.assertEqual(exported_plan["article_code"], "PLAN-PREMIUM")
        self.assertIsNotNone(exported_plan["created"])

    def test_export_plan_components(self):
        """Test that export captures plan components with pricing."""
        offering = marketplace_factories.OfferingFactory()
        plan = marketplace_factories.PlanFactory(offering=offering, name="Test Plan")
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering, type="cpu", name="CPU"
        )
        marketplace_factories.PlanComponentFactory(
            plan=plan, component=component, amount=4, price=50.25, future_price=55.0
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("plan_components", data)
        self.assertGreater(len(data["plan_components"]), 0)

        exported_pc = [
            pc
            for pc in data["plan_components"]
            if pc["plan_uuid"] == plan.uuid.hex
            and pc["component_uuid"] == component.uuid.hex
        ][0]
        self.assertEqual(exported_pc["plan_uuid"], plan.uuid.hex)
        self.assertEqual(exported_pc["plan_name"], "Test Plan")
        self.assertEqual(exported_pc["component_uuid"], component.uuid.hex)
        self.assertEqual(exported_pc["component_type"], "cpu")
        self.assertEqual(exported_pc["component_name"], "CPU")
        self.assertEqual(exported_pc["amount"], 4)
        self.assertEqual(float(exported_pc["price"]), 50.25)
        self.assertEqual(float(exported_pc["future_price"]), 55.0)

    def test_export_invoices(self):
        """Test that export captures invoices with all fields."""
        customer = structure_factories.CustomerFactory(name="Invoice Customer")
        invoice = invoices_factories.InvoiceFactory(
            customer=customer,
            month=3,
            year=2024,
            state="created",
            total_cost=1000.50,
            total_price=1200.60,
            tax_percent=20.0,
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("invoices", data)
        self.assertEqual(len(data["invoices"]), 1)

        exported_invoice = data["invoices"][0]
        self.assertEqual(exported_invoice["uuid"], invoice.uuid.hex)
        self.assertEqual(exported_invoice["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_invoice["customer_name"], "Invoice Customer")
        self.assertEqual(exported_invoice["month"], 3)
        self.assertEqual(exported_invoice["year"], 2024)
        self.assertEqual(exported_invoice["state"], "created")
        self.assertEqual(exported_invoice["total_cost"], "1000.50")
        self.assertEqual(exported_invoice["total_price"], "1200.60")
        self.assertEqual(exported_invoice["tax_percent"], "20.00")
        self.assertIsNotNone(exported_invoice["invoice_date"])

    def test_export_invoice_items(self):
        """Test that export captures invoice items with all fields."""
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)
        invoice = invoices_factories.InvoiceFactory(customer=customer)
        resource = marketplace_factories.ResourceFactory(project=project)
        invoice_item = invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            resource=resource,
            project=project,
            name="Test Invoice Item",
            quantity=10.5,
            measured_unit="hours",
            unit_price=25.0,
            article_code="ITEM-001",
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("invoice_items", data)
        self.assertEqual(len(data["invoice_items"]), 1)

        exported_item = data["invoice_items"][0]
        self.assertEqual(exported_item["uuid"], invoice_item.uuid.hex)
        self.assertEqual(exported_item["invoice_uuid"], invoice.uuid.hex)
        self.assertEqual(exported_item["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_item["customer_name"], customer.name)
        self.assertEqual(exported_item["resource_uuid"], resource.uuid.hex)
        self.assertEqual(exported_item["resource_name"], resource.name)
        self.assertEqual(exported_item["project_uuid"], project.uuid.hex)
        self.assertEqual(exported_item["project_name"], project.name)
        self.assertEqual(exported_item["name"], "Test Invoice Item")
        self.assertEqual(float(exported_item["quantity"]), 10.5)
        self.assertEqual(exported_item["measured_unit"], "hours")
        self.assertEqual(float(exported_item["unit_price"]), 25.0)
        self.assertEqual(exported_item["article_code"], "ITEM-001")
        self.assertIsNotNone(exported_item["start"])
        self.assertIsNotNone(exported_item["end"])

    def test_export_orders(self):
        """Test that export captures orders with all fields."""
        customer = structure_factories.CustomerFactory(name="Order Customer")
        project = structure_factories.ProjectFactory(
            name="Order Project", customer=customer
        )
        offering = marketplace_factories.OfferingFactory(name="Test Offering")
        plan = marketplace_factories.PlanFactory(offering=offering, name="Test Plan")
        old_plan = marketplace_factories.PlanFactory(offering=offering, name="Old Plan")
        resource = marketplace_factories.ResourceFactory(
            name="Test Resource", offering=offering, plan=plan, project=project
        )
        created_by = structure_factories.UserFactory(username="creator")
        consumer_reviewer = structure_factories.UserFactory(
            username="consumer_reviewer"
        )
        provider_reviewer = structure_factories.UserFactory(
            username="provider_reviewer"
        )

        # Create order with all optional fields
        order1 = marketplace_factories.OrderFactory(
            project=project,
            resource=resource,
            offering=offering,
            plan=plan,
            created_by=created_by,
            old_plan=old_plan,
            consumer_reviewed_by=consumer_reviewer,
            provider_reviewed_by=provider_reviewer,
            consumer_reviewed_at=timezone.now(),
            provider_reviewed_at=timezone.now(),
            output="Order output data",
            callback_url="https://example.com/callback",
            termination_comment="Termination reason",
            request_comment="Request details",
            attributes={"key": "value"},
            limits={"cpu": 4, "ram": 8192},
        )

        # Create order without optional fields
        order2 = marketplace_factories.OrderFactory(
            project=project,
            resource=resource,
            offering=offering,
            plan=None,
            created_by=created_by,
            old_plan=None,
            consumer_reviewed_by=None,
            provider_reviewed_by=None,
            attributes={},
            limits={},
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("orders", data)
        self.assertEqual(len(data["orders"]), 2)

        # Find exported orders by UUID
        exported_order1 = next(
            o for o in data["orders"] if o["uuid"] == order1.uuid.hex
        )
        exported_order2 = next(
            o for o in data["orders"] if o["uuid"] == order2.uuid.hex
        )

        # Verify order1 with all optional fields
        self.assertEqual(exported_order1["uuid"], order1.uuid.hex)
        self.assertEqual(exported_order1["project_uuid"], project.uuid.hex)
        self.assertEqual(exported_order1["resource_uuid"], resource.uuid.hex)
        self.assertEqual(exported_order1["offering_uuid"], offering.uuid.hex)
        self.assertEqual(exported_order1["plan_uuid"], plan.uuid.hex)
        self.assertEqual(exported_order1["created_by_uuid"], created_by.uuid.hex)
        self.assertEqual(exported_order1["old_plan_uuid"], old_plan.uuid.hex)
        self.assertEqual(
            exported_order1["consumer_reviewed_by_uuid"], consumer_reviewer.uuid.hex
        )
        self.assertEqual(
            exported_order1["provider_reviewed_by_uuid"], provider_reviewer.uuid.hex
        )
        self.assertIsNotNone(exported_order1["consumer_reviewed_at"])
        self.assertIsNotNone(exported_order1["provider_reviewed_at"])
        self.assertEqual(exported_order1["output"], "Order output data")
        self.assertEqual(
            exported_order1["callback_url"], "https://example.com/callback"
        )
        self.assertEqual(exported_order1["termination_comment"], "Termination reason")
        self.assertEqual(exported_order1["request_comment"], "Request details")
        self.assertEqual(exported_order1["attributes"], {"key": "value"})
        self.assertEqual(exported_order1["limits"], {"cpu": 4, "ram": 8192})

        # Verify order2 with null optional fields
        self.assertEqual(exported_order2["uuid"], order2.uuid.hex)
        self.assertEqual(exported_order2["offering_uuid"], offering.uuid.hex)
        self.assertIsNone(exported_order2["plan_uuid"])
        self.assertIsNone(exported_order2["old_plan_uuid"])
        self.assertIsNone(exported_order2["consumer_reviewed_by_uuid"])
        self.assertIsNone(exported_order2["provider_reviewed_by_uuid"])
        self.assertIsNone(exported_order2["consumer_reviewed_at"])
        self.assertIsNone(exported_order2["provider_reviewed_at"])
        self.assertEqual(exported_order2["attributes"], {})
        self.assertEqual(exported_order2["limits"], {})

    # Round-trip Test

    def test_export_then_import_preserves_data(self):
        """Test that exporting then importing preserves all data correctly."""
        # Create test data
        user = structure_factories.UserFactory(
            username="roundtrip_user",
            email="roundtrip@example.com",
            full_name="Roundtrip User",
        )
        customer = structure_factories.CustomerFactory(
            name="Roundtrip Customer", abbreviation="RC"
        )
        project = structure_factories.ProjectFactory(
            name="Roundtrip Project", customer=customer
        )
        category = marketplace_factories.CategoryFactory(title="Roundtrip Category")

        # Store original UUIDs
        original_user_uuid = user.uuid
        original_customer_uuid = customer.uuid
        original_project_uuid = project.uuid
        original_category_uuid = category.uuid

        # Export data
        self._call_export_command()

        # Verify export file exists and has data
        data = self._load_exported_json()
        self.assertGreater(len(data["users"]), 0)
        self.assertGreater(len(data["customers"]), 0)
        self.assertGreater(len(data["projects"]), 0)
        self.assertGreater(len(data["categories"]), 0)

        # Delete all objects
        from waldur_core.core.models import User
        from waldur_core.structure.models import Customer, Project
        from waldur_mastermind.marketplace.models import Category

        User.objects.all().delete()
        Project.objects.all().delete()
        Customer.objects.all().delete()
        Category.objects.all().delete()

        # Import data back
        import_output = StringIO()
        call_command(
            "import_structure", "-i", self.output_file_path, stdout=import_output
        )

        # Verify data restored
        from waldur_core.core.models import User

        restored_user = User.objects.get(uuid=original_user_uuid)
        self.assertEqual(restored_user.username, "roundtrip_user")
        self.assertEqual(restored_user.email, "roundtrip@example.com")

        from waldur_core.structure.models import Customer

        restored_customer = Customer.objects.get(uuid=original_customer_uuid)
        self.assertEqual(restored_customer.name, "Roundtrip Customer")

        from waldur_core.structure.models import Project

        restored_project = Project.available_objects.get(uuid=original_project_uuid)
        self.assertEqual(restored_project.name, "Roundtrip Project")
        self.assertEqual(restored_project.customer.uuid, original_customer_uuid)

        from waldur_mastermind.marketplace.models import Category

        restored_category = Category.objects.get(uuid=original_category_uuid)
        self.assertEqual(restored_category.title, "Roundtrip Category")

    def test_export_import_slurm_qos_round_trip(self):
        """SLURM QoS profiles and partition allow-list links survive round-trip."""
        from waldur_mastermind.marketplace.models import (
            SlurmOfferingQoS,
            SlurmPartitionQoS,
        )

        offering = marketplace_factories.OfferingFactory(name="QoS Offering")
        partition = marketplace_factories.OfferingPartitionFactory(
            offering=offering, partition_name="gpu"
        )
        qos = marketplace_factories.SlurmOfferingQoSFactory(
            offering=offering, name="boost", max_nodes=128
        )
        link = marketplace_factories.SlurmPartitionQoSFactory(
            partition=partition, qos=qos, is_default=True
        )
        qos_uuid = qos.uuid
        link_uuid = link.uuid

        # Export
        self._call_export_command()
        data = self._load_exported_json()
        self.assertEqual(len(data["slurm_offering_qos"]), 1)
        self.assertEqual(data["slurm_offering_qos"][0]["name"], "boost")
        self.assertEqual(data["slurm_offering_qos"][0]["max_nodes"], 128)
        self.assertEqual(len(data["slurm_partition_qos"]), 1)
        self.assertTrue(data["slurm_partition_qos"][0]["is_default"])

        # Drop the QoS rows (partition + offering are kept).
        SlurmPartitionQoS.objects.all().delete()
        SlurmOfferingQoS.objects.all().delete()

        # Re-import
        import_output = StringIO()
        call_command(
            "import_structure", "-i", self.output_file_path, stdout=import_output
        )

        restored_qos = SlurmOfferingQoS.objects.get(uuid=qos_uuid)
        self.assertEqual(restored_qos.name, "boost")
        self.assertEqual(restored_qos.max_nodes, 128)
        self.assertEqual(restored_qos.offering.uuid, offering.uuid)

        restored_link = SlurmPartitionQoS.objects.get(uuid=link_uuid)
        self.assertTrue(restored_link.is_default)
        self.assertEqual(restored_link.partition.uuid, partition.uuid)
        self.assertEqual(restored_link.qos.uuid, restored_qos.uuid)

    def test_export_group_invitations(self):
        """Test that export captures group invitation data."""
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.permissions.models import Role
        from waldur_core.users.models import GroupInvitation

        customer = structure_factories.CustomerFactory()
        user = structure_factories.UserFactory()
        content_type = ContentType.objects.get_for_model(customer)
        role = Role.objects.create(
            name="CUSTOMER.OWNER",
            description="Customer Owner",
            content_type=content_type,
            is_active=True,
        )

        group_invitation = GroupInvitation.objects.create(
            customer=customer,
            role=role,
            created_by=user,
            is_active=True,
            is_public=False,
            auto_create_project=False,
            content_type=content_type,
            object_id=customer.id,
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify group invitations exported
        self.assertIn("group_invitations", data)
        exported_group_invitations = [
            gi
            for gi in data["group_invitations"]
            if gi["uuid"] == group_invitation.uuid.hex
        ]
        self.assertEqual(len(exported_group_invitations), 1)

        exported_gi = exported_group_invitations[0]
        self.assertEqual(exported_gi["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_gi["role_uuid"], role.uuid.hex)
        self.assertEqual(exported_gi["created_by_uuid"], user.uuid.hex)
        self.assertEqual(exported_gi["is_active"], True)
        self.assertEqual(exported_gi["is_public"], False)
        self.assertEqual(exported_gi["auto_create_project"], False)

    def test_export_invitations(self):
        """Test that export captures invitation data."""
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.permissions.models import Role
        from waldur_core.users.models import Invitation

        customer = structure_factories.CustomerFactory()
        user = structure_factories.UserFactory()
        content_type = ContentType.objects.get_for_model(customer)
        role = Role.objects.create(
            name="CUSTOMER.OWNER",
            description="Customer Owner",
            content_type=content_type,
            is_active=True,
        )

        invitation = Invitation.objects.create(
            customer=customer,
            role=role,
            created_by=user,
            email="test@example.com",
            full_name="Test User",
            state="pending",
            execution_state="Scheduled",
            content_type=content_type,
            object_id=customer.id,
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify invitations exported
        self.assertIn("invitations", data)
        exported_invitations = [
            inv for inv in data["invitations"] if inv["uuid"] == invitation.uuid.hex
        ]
        self.assertEqual(len(exported_invitations), 1)

        exported_inv = exported_invitations[0]
        self.assertEqual(exported_inv["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_inv["role_uuid"], role.uuid.hex)
        self.assertEqual(exported_inv["created_by_uuid"], user.uuid.hex)
        self.assertEqual(exported_inv["email"], "test@example.com")
        self.assertEqual(exported_inv["full_name"], "Test User")
        self.assertEqual(exported_inv["state"], "pending")
        self.assertEqual(exported_inv["execution_state"], "Scheduled")

    def test_export_permission_requests(self):
        """Test that export captures permission request data."""
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.core.enums import ReviewStates
        from waldur_core.permissions.models import Role
        from waldur_core.users.models import GroupInvitation, PermissionRequest

        customer = structure_factories.CustomerFactory()
        user = structure_factories.UserFactory()
        requesting_user = structure_factories.UserFactory()
        content_type = ContentType.objects.get_for_model(customer)
        role = Role.objects.create(
            name="CUSTOMER.OWNER",
            description="Customer Owner",
            content_type=content_type,
            is_active=True,
        )

        group_invitation = GroupInvitation.objects.create(
            customer=customer,
            role=role,
            created_by=user,
            is_active=True,
            is_public=False,
            auto_create_project=False,
            content_type=content_type,
            object_id=customer.id,
        )

        permission_request = PermissionRequest.objects.create(
            invitation=group_invitation,
            created_by=requesting_user,
            state=ReviewStates.PENDING,
            review_comment="Please grant me access",
        )

        self._call_export_command()
        data = self._load_exported_json()

        # Verify permission requests exported
        self.assertIn("permission_requests", data)
        exported_permission_requests = [
            pr
            for pr in data["permission_requests"]
            if pr["uuid"] == permission_request.uuid.hex
        ]
        self.assertEqual(len(exported_permission_requests), 1)

        exported_pr = exported_permission_requests[0]
        self.assertEqual(exported_pr["invitation_uuid"], group_invitation.uuid.hex)
        self.assertEqual(exported_pr["created_by_uuid"], requesting_user.uuid.hex)
        self.assertEqual(exported_pr["state"], ReviewStates.PENDING)
        self.assertEqual(exported_pr["review_comment"], "Please grant me access")

    # Credit Export Tests

    def test_export_customer_credits_with_all_fields(self):
        """Test that customer credits are exported with all fields."""
        # Create test data
        customer = structure_factories.CustomerFactory()
        offering = marketplace_factories.OfferingFactory()

        customer_credit = invoices_factories.CustomerCreditFactory(
            customer=customer,
            value=1000.50,
            expected_consumption=800.25,
            minimal_consumption_logic="linear",
            grace_coefficient=15,
            apply_as_minimal_consumption=True,
            end_date=timezone.now().replace(day=1).date(),
        )
        # Add offering to many-to-many relationship
        customer_credit.offerings.add(offering)

        # Export and verify
        self._call_export_command()
        exported_data = self._load_exported_json()

        self.assertIn("customer_credits", exported_data)
        exported_credits = exported_data["customer_credits"]
        self.assertEqual(len(exported_credits), 1)

        exported_credit = exported_credits[0]
        self.assertEqual(exported_credit["uuid"], customer_credit.uuid.hex)
        self.assertEqual(exported_credit["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_credit["customer_name"], customer.name)
        self.assertEqual(exported_credit["value"], "1000.50000")
        self.assertEqual(exported_credit["expected_consumption"], "800.25000")
        self.assertEqual(exported_credit["minimal_consumption_logic"], "linear")
        self.assertEqual(exported_credit["grace_coefficient"], "15")
        self.assertEqual(exported_credit["apply_as_minimal_consumption"], True)
        self.assertIsNotNone(exported_credit["end_date"])
        self.assertIsNotNone(exported_credit["created"])
        self.assertIsNotNone(exported_credit["modified"])
        self.assertIn("offering_uuids", exported_credit)
        self.assertEqual(len(exported_credit["offering_uuids"]), 1)
        self.assertEqual(exported_credit["offering_uuids"][0], offering.uuid.hex)

    def test_export_project_credits_with_all_fields(self):
        """Test that project credits are exported with all fields."""
        # Create test data
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)
        # ProjectCredit requires a CustomerCredit to exist with sufficient value
        invoices_factories.CustomerCreditFactory(
            customer=customer,
            value=1000,  # Higher than project credit value
        )

        project_credit = invoices_factories.ProjectCreditFactory(
            project=project,
            value=500.75,
            expected_consumption=400.50,
            minimal_consumption_logic="fixed",
            grace_coefficient=10,
            apply_as_minimal_consumption=False,
            end_date=timezone.now().replace(day=1).date(),
            mark_unused_credit_as_spent_on_project_termination=True,
        )

        # Export and verify
        self._call_export_command()
        exported_data = self._load_exported_json()

        self.assertIn("project_credits", exported_data)
        exported_credits = exported_data["project_credits"]
        self.assertEqual(len(exported_credits), 1)

        exported_credit = exported_credits[0]
        self.assertEqual(exported_credit["uuid"], project_credit.uuid.hex)
        self.assertEqual(exported_credit["project_uuid"], project.uuid.hex)
        self.assertEqual(exported_credit["project_name"], project.name)
        self.assertEqual(exported_credit["customer_uuid"], customer.uuid.hex)
        self.assertEqual(exported_credit["customer_name"], customer.name)
        self.assertEqual(exported_credit["value"], "500.75000")
        self.assertEqual(exported_credit["expected_consumption"], "400.50000")
        self.assertEqual(exported_credit["minimal_consumption_logic"], "fixed")
        self.assertEqual(exported_credit["grace_coefficient"], "10")
        self.assertEqual(exported_credit["apply_as_minimal_consumption"], False)
        self.assertIsNotNone(exported_credit["end_date"])
        self.assertEqual(
            exported_credit["mark_unused_credit_as_spent_on_project_termination"], True
        )
        self.assertIsNotNone(exported_credit["created"])
        self.assertIsNotNone(exported_credit["modified"])

    def test_export_credits_empty_collections(self):
        """Test that empty credit collections are properly handled."""
        # Export without any credits
        self._call_export_command()
        exported_data = self._load_exported_json()

        self.assertIn("customer_credits", exported_data)
        self.assertIn("project_credits", exported_data)
        self.assertEqual(len(exported_data["customer_credits"]), 0)
        self.assertEqual(len(exported_data["project_credits"]), 0)

    def test_export_multiple_credits_different_customers_projects(self):
        """Test exporting multiple credits across different customers and projects."""
        # Create test data
        customer1 = structure_factories.CustomerFactory()
        customer2 = structure_factories.CustomerFactory()
        project1 = structure_factories.ProjectFactory(customer=customer1)
        project2 = structure_factories.ProjectFactory(customer=customer2)

        invoices_factories.CustomerCreditFactory(customer=customer1, value=1000)
        invoices_factories.CustomerCreditFactory(customer=customer2, value=2000)
        invoices_factories.ProjectCreditFactory(project=project1, value=300)
        invoices_factories.ProjectCreditFactory(project=project2, value=400)

        # Export and verify
        self._call_export_command()
        exported_data = self._load_exported_json()

        # Check customer credits
        customer_credits = exported_data["customer_credits"]
        self.assertEqual(len(customer_credits), 2)
        exported_customer_uuids = {
            credit["customer_uuid"] for credit in customer_credits
        }
        self.assertIn(customer1.uuid.hex, exported_customer_uuids)
        self.assertIn(customer2.uuid.hex, exported_customer_uuids)

        # Check project credits
        project_credits = exported_data["project_credits"]
        self.assertEqual(len(project_credits), 2)
        exported_project_uuids = {credit["project_uuid"] for credit in project_credits}
        self.assertIn(project1.uuid.hex, exported_project_uuids)
        self.assertIn(project2.uuid.hex, exported_project_uuids)

    def test_export_customer_credit_without_offerings(self):
        """Test that customer credits without offerings are exported correctly."""
        customer = structure_factories.CustomerFactory()
        invoices_factories.CustomerCreditFactory(customer=customer)

        # Export and verify
        self._call_export_command()
        exported_data = self._load_exported_json()

        exported_credits = exported_data["customer_credits"]
        self.assertEqual(len(exported_credits), 1)

        exported_credit = exported_credits[0]
        # Should not have offering_uuids key since no offerings are associated
        self.assertNotIn("offering_uuids", exported_credit)

    def test_export_customer_credit_with_multiple_offerings(self):
        """Test that customer credits with multiple offerings are exported correctly."""
        customer = structure_factories.CustomerFactory()
        offering1 = marketplace_factories.OfferingFactory()
        offering2 = marketplace_factories.OfferingFactory()

        customer_credit = invoices_factories.CustomerCreditFactory(customer=customer)
        customer_credit.offerings.add(offering1, offering2)

        # Export and verify
        self._call_export_command()
        exported_data = self._load_exported_json()

        exported_credits = exported_data["customer_credits"]
        self.assertEqual(len(exported_credits), 1)

        exported_credit = exported_credits[0]
        self.assertIn("offering_uuids", exported_credit)
        self.assertEqual(len(exported_credit["offering_uuids"]), 2)
        self.assertIn(offering1.uuid.hex, exported_credit["offering_uuids"])
        self.assertIn(offering2.uuid.hex, exported_credit["offering_uuids"])

    def test_export_credits_command_output_shows_counts(self):
        """Test that the export command output shows credit counts."""
        # Create test data
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)
        invoices_factories.CustomerCreditFactory(customer=customer, value=1000)
        invoices_factories.ProjectCreditFactory(project=project, value=100)

        # Call export and capture output
        output = self._call_export_command()

        # Verify that credit counts appear in the summary
        self.assertIn("Customer Credits: 1", output)
        self.assertIn("Project Credits: 1", output)

    def test_export_software_catalogs(self):
        """Test that export captures software catalog definitions."""
        catalog = marketplace_factories.SoftwareCatalogFactory(
            name="EESSI",
            version="2023.06",
            catalog_type="binary_runtime",
            source_url="https://eessi.io",
            description="EESSI software catalog",
            metadata={"arch_mapping": {"x86_64": "generic"}},
            auto_update_enabled=True,
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("software_catalogs", data)
        self.assertEqual(len(data["software_catalogs"]), 1)

        exported_catalog = data["software_catalogs"][0]
        self.assertEqual(exported_catalog["uuid"], catalog.uuid.hex)
        self.assertEqual(exported_catalog["name"], "EESSI")
        self.assertEqual(exported_catalog["version"], "2023.06")
        self.assertEqual(exported_catalog["catalog_type"], "binary_runtime")
        self.assertEqual(exported_catalog["source_url"], "https://eessi.io")
        self.assertEqual(exported_catalog["description"], "EESSI software catalog")
        self.assertEqual(
            exported_catalog["metadata"], {"arch_mapping": {"x86_64": "generic"}}
        )
        self.assertTrue(exported_catalog["auto_update_enabled"])
        self.assertIsNotNone(exported_catalog["created"])

    def test_export_offering_partitions(self):
        """Test that export captures offering partition data."""
        offering = marketplace_factories.OfferingFactory(name="SLURM Offering")
        partition = marketplace_factories.OfferingPartitionFactory(
            offering=offering,
            partition_name="gpu",
            cpu_bind=1,
            def_cpu_per_gpu=4,
            max_cpus_per_node=64,
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("offering_partitions", data)
        self.assertEqual(len(data["offering_partitions"]), 1)

        exported_partition = data["offering_partitions"][0]
        self.assertEqual(exported_partition["uuid"], partition.uuid.hex)
        self.assertEqual(exported_partition["offering_uuid"], offering.uuid.hex)
        self.assertEqual(exported_partition["offering_name"], "SLURM Offering")
        self.assertEqual(exported_partition["partition_name"], "gpu")
        self.assertEqual(exported_partition["cpu_bind"], 1)
        self.assertEqual(exported_partition["def_cpu_per_gpu"], 4)
        self.assertEqual(exported_partition["max_cpus_per_node"], 64)
        self.assertIsNotNone(exported_partition["created"])

    def test_export_offering_software_catalogs(self):
        """Test that export captures offering-to-software-catalog links."""
        offering = marketplace_factories.OfferingFactory(name="Test Offering")
        catalog = marketplace_factories.SoftwareCatalogFactory(
            name="EESSI", version="2023.06"
        )
        partition = marketplace_factories.OfferingPartitionFactory(
            offering=offering, partition_name="gpu"
        )
        link = marketplace_factories.OfferingSoftwareCatalogFactory(
            offering=offering,
            catalog=catalog,
            partition=partition,
            enabled_cpu_family=["x86_64", "aarch64"],
            enabled_cpu_microarchitectures=["generic", "zen3"],
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("offering_software_catalogs", data)
        self.assertEqual(len(data["offering_software_catalogs"]), 1)

        exported_link = data["offering_software_catalogs"][0]
        self.assertEqual(exported_link["uuid"], link.uuid.hex)
        self.assertEqual(exported_link["offering_uuid"], offering.uuid.hex)
        self.assertEqual(exported_link["offering_name"], "Test Offering")
        self.assertEqual(exported_link["catalog_uuid"], catalog.uuid.hex)
        self.assertEqual(exported_link["catalog_name"], "EESSI")
        self.assertEqual(exported_link["partition_uuid"], partition.uuid.hex)
        self.assertEqual(exported_link["enabled_cpu_family"], ["x86_64", "aarch64"])
        self.assertEqual(
            exported_link["enabled_cpu_microarchitectures"], ["generic", "zen3"]
        )
        self.assertIsNotNone(exported_link["created"])

    def test_export_offering_software_catalogs_without_partition(self):
        """Test that export handles links without partition correctly."""
        offering = marketplace_factories.OfferingFactory()
        catalog = marketplace_factories.SoftwareCatalogFactory()
        link = marketplace_factories.OfferingSoftwareCatalogFactory(
            offering=offering,
            catalog=catalog,
            partition=None,
            enabled_cpu_family=["x86_64"],
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("offering_software_catalogs", data)
        exported_link = data["offering_software_catalogs"][0]
        self.assertEqual(exported_link["uuid"], link.uuid.hex)
        self.assertNotIn("partition_uuid", exported_link)

    def test_export_software_catalogs_empty(self):
        """Test that empty software catalog collections are handled correctly."""
        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("software_catalogs", data)
        self.assertIn("offering_partitions", data)
        self.assertIn("offering_software_catalogs", data)
        self.assertEqual(len(data["software_catalogs"]), 0)
        self.assertEqual(len(data["offering_partitions"]), 0)
        self.assertEqual(len(data["offering_software_catalogs"]), 0)

    def test_export_project_estimated_cost_policies(self):
        """Test that project estimated cost policies are exported correctly."""
        policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            limit_cost=500,
            actions="notify_project_team,block_creation_of_new_resources",
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("project_estimated_cost_policies", data)
        policies = data["project_estimated_cost_policies"]
        self.assertEqual(len(policies), 1)
        exported = policies[0]
        self.assertEqual(exported["uuid"], policy.uuid.hex)
        self.assertEqual(exported["project_uuid"], policy.scope.uuid.hex)
        self.assertEqual(exported["limit_cost"], 500)
        self.assertEqual(
            exported["actions"],
            "notify_project_team,block_creation_of_new_resources",
        )

    def test_export_customer_estimated_cost_policies(self):
        """Test that customer estimated cost policies are exported correctly."""
        policy = policy_factories.CustomerEstimatedCostPolicyFactory(
            limit_cost=1000,
            actions="notify_organization_owners",
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("customer_estimated_cost_policies", data)
        policies = data["customer_estimated_cost_policies"]
        self.assertEqual(len(policies), 1)
        exported = policies[0]
        self.assertEqual(exported["uuid"], policy.uuid.hex)
        self.assertEqual(exported["customer_uuid"], policy.scope.uuid.hex)
        self.assertEqual(exported["limit_cost"], 1000)
        self.assertEqual(exported["actions"], "notify_organization_owners")

    def test_export_slurm_periodic_policies(self):
        """Test that SLURM periodic usage policies are exported correctly."""
        policy = policy_factories.SlurmPeriodicUsagePolicyFactory(
            limit_type="GrpTRESMins",
            carryover_factor=75,
            grace_ratio=0.3,
        )

        self._call_export_command()
        data = self._load_exported_json()

        self.assertIn("slurm_periodic_policies", data)
        policies = data["slurm_periodic_policies"]
        self.assertEqual(len(policies), 1)
        exported = policies[0]
        self.assertEqual(exported["uuid"], policy.uuid.hex)
        self.assertEqual(exported["offering_uuid"], policy.scope.uuid.hex)
        self.assertEqual(exported["limit_type"], "GrpTRESMins")
        self.assertEqual(exported["carryover_factor"], 75)
        self.assertAlmostEqual(exported["grace_ratio"], 0.3)
        self.assertIn("component_limits", exported)

    def test_export_events_not_included_by_default(self):
        """Test that events are not exported without --include-events flag."""
        from waldur_core.logging.tests.factories import EventFactory

        EventFactory(event_type="reduction_of_customer_credit")

        self._call_export_command()
        data = self._load_exported_json()

        self.assertNotIn("events", data)

    def test_export_events_with_flag(self):
        """Test that events related to invoicing, credits and policies are exported."""
        from waldur_core.logging.tests.factories import EventFactory

        credit_event = EventFactory(
            event_type="reduction_of_customer_credit",
            message="Credit reduced",
            context={"customer_uuid": "abc123"},
        )
        invoice_event = EventFactory(
            event_type="invoice_created",
            message="Invoice created",
            context={"customer_uuid": "abc123"},
        )
        policy_event = EventFactory(
            event_type="policy_notification",
            message="Policy triggered",
            context={"project_uuid": "def456"},
        )
        # Unrelated event should not be exported
        EventFactory(
            event_type="auth_logged_in_with_username",
            message="User logged in",
        )

        self._call_export_command(include_events=True)
        data = self._load_exported_json()

        self.assertIn("events", data)
        exported_events = data["events"]
        exported_uuids = {e["uuid"] for e in exported_events}

        self.assertIn(credit_event.uuid.hex, exported_uuids)
        self.assertIn(invoice_event.uuid.hex, exported_uuids)
        self.assertIn(policy_event.uuid.hex, exported_uuids)
        self.assertEqual(len(exported_events), 3)

        # Verify event fields
        credit_exported = next(
            e for e in exported_events if e["uuid"] == credit_event.uuid.hex
        )
        self.assertEqual(credit_exported["event_type"], "reduction_of_customer_credit")
        self.assertEqual(credit_exported["message"], "Credit reduced")
        self.assertEqual(credit_exported["context"]["customer_uuid"], "abc123")
        self.assertIn("created", credit_exported)

    def test_export_includes_events_from_overdue_credit_zeroing(self):
        """Test that events generated by set_to_zero_overdue_credits are included in export."""
        from datetime import timedelta

        from waldur_mastermind.invoices.tasks import set_to_zero_overdue_credits

        customer = structure_factories.CustomerFactory()
        past_first_of_month = (
            timezone.now().date().replace(day=1) - timedelta(days=1)
        ).replace(day=1)
        credit = invoices_factories.CustomerCreditFactory(
            customer=customer,
            value=500,
            end_date=past_first_of_month,
        )

        self.assertEqual(credit.value, 500)

        set_to_zero_overdue_credits()

        credit.refresh_from_db()
        self.assertEqual(credit.value, 0)

        self._call_export_command(include_events=True)
        data = self._load_exported_json()

        self.assertIn("events", data)
        exported_events = data["events"]
        overdue_events = [
            e
            for e in exported_events
            if e["event_type"] == "set_to_zero_overdue_credit"
        ]
        self.assertEqual(len(overdue_events), 1)
        self.assertIn("set to zero", overdue_events[0]["message"])
        self.assertEqual(
            overdue_events[0]["context"]["customer_uuid"], customer.uuid.hex
        )
