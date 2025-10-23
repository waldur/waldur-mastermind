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

    def _call_export_command(self, output_path=None):
        """Helper to call export_structure command and return output."""
        if output_path is None:
            output_path = self.output_file_path

        output = StringIO()
        call_command("export_structure", "-o", output_path, stdout=output)
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
            default_tenant_category=False,
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
        self.assertEqual(exported_user_role["scope_uuid"], str(customer.id))
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
