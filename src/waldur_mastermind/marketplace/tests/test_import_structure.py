import json
import os
import shutil
import tempfile
from io import StringIO

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase

from waldur_core.core.models import User
from waldur_core.permissions.models import Role, RolePermission, UserRole
from waldur_core.structure.models import Customer, Project
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.models import Invoice, InvoiceItem
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.models import (
    Category,
    ComponentUsage,
    Offering,
    OfferingComponent,
    Order,
    Plan,
    PlanComponent,
    Resource,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class ImportStructureCommandTest(TestCase):
    """Test suite for import_structure management command."""

    def setUp(self):
        """Set up test fixtures and create test data."""
        self.temp_dir = tempfile.mkdtemp()
        self.test_file_path = os.path.join(self.temp_dir, "test_structure.json")

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _create_test_json(self, data):
        """Helper to write test data to JSON file."""
        with open(self.test_file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _call_import_command(self, *args, **kwargs):
        """Helper to call import_structure command and return output."""
        output = StringIO()
        kwargs.setdefault("stdout", output)
        call_command("import_structure", *args, **kwargs)
        return output.getvalue()

    # Basic Import Tests

    def test_import_users_creates_new_users(self):
        """Test that importing users creates new user objects."""
        users_data = [
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "username": "testuser1",
                "email": "test1@example.com",
                "full_name": "Test User One",
                "is_staff": False,
                "is_support": False,
                "is_active": True,
            },
            {
                "uuid": "22222222-2222-2222-2222-222222222222",
                "username": "testuser2",
                "email": "test2@example.com",
                "full_name": "Test User Two",
                "native_name": "Test Native",
                "phone_number": "+123456789",
                "organization": "Test Org",
                "job_title": "Developer",
                "is_staff": True,
                "is_support": False,
                "is_active": True,
            },
        ]

        data = {"users": users_data}
        self._create_test_json(data)

        output = self._call_import_command("-i", self.test_file_path)

        # Verify users created
        self.assertEqual(User.objects.count(), 2)

        user1 = User.objects.get(uuid="11111111-1111-1111-1111-111111111111")
        self.assertEqual(user1.username, "testuser1")
        self.assertEqual(user1.email, "test1@example.com")
        self.assertEqual(user1.full_name, "Test User One")
        self.assertFalse(user1.has_usable_password())

        user2 = User.objects.get(uuid="22222222-2222-2222-2222-222222222222")
        self.assertEqual(user2.username, "testuser2")
        self.assertEqual(user2.native_name, "Test Native")
        self.assertEqual(user2.phone_number, "+123456789")
        self.assertEqual(user2.organization, "Test Org")
        self.assertEqual(user2.job_title, "Developer")
        self.assertTrue(user2.is_staff)

        # Verify summary output
        self.assertIn("Created: 2", output)

    def test_import_customers_creates_new_customers(self):
        """Test that importing customers creates new customer objects."""
        customers_data = [
            {
                "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "name": "Customer One",
                "native_name": "Native Customer One",
                "abbreviation": "C1",
                "email": "customer1@example.com",
                "phone_number": "+111111111",
                "country": "EE",
                "vat_code": "VAT123",
                "registration_code": "REG123",
                "blocked": False,
                "archived": False,
            }
        ]

        data = {"customers": customers_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify customer created
        self.assertEqual(Customer.objects.count(), 1)

        customer = Customer.objects.get(uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(customer.name, "Customer One")
        self.assertEqual(customer.native_name, "Native Customer One")
        self.assertEqual(customer.abbreviation, "C1")
        self.assertEqual(customer.email, "customer1@example.com")
        self.assertEqual(customer.country, "EE")
        self.assertEqual(customer.vat_code, "VAT123")
        self.assertFalse(customer.blocked)

    def test_import_projects_with_customer_relationship(self):
        """Test that importing projects creates projects with correct customer FK."""
        # Create customer first
        customer = structure_factories.CustomerFactory(
            uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )

        projects_data = [
            {
                "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "name": "Test Project",
                "description": "Test project description",
                "customer_uuid": str(customer.uuid),
                "oecd_fos_2007_code": "1.1",
            }
        ]

        data = {"projects": projects_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify project created with correct customer
        self.assertEqual(Project.available_objects.count(), 1)

        project = Project.available_objects.get(
            uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        )
        self.assertEqual(project.name, "Test Project")
        self.assertEqual(project.description, "Test project description")
        self.assertEqual(project.customer, customer)
        self.assertEqual(project.oecd_fos_2007_code, "1.1")

    def test_import_categories(self):
        """Test that importing categories creates category objects."""
        categories_data = [
            {
                "uuid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "title": "Virtual Machines",
                "description": "VM category",
                "backend_id": "vm",
                "default_vm_category": True,
                "default_volume_category": False,
                "default_tenant_category": False,
            }
        ]

        data = {"categories": categories_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify category created
        self.assertEqual(Category.objects.count(), 1)

        category = Category.objects.get(uuid="cccccccc-cccc-cccc-cccc-cccccccccccc")
        self.assertEqual(category.title, "Virtual Machines")
        self.assertEqual(category.description, "VM category")
        self.assertEqual(category.backend_id, "vm")
        self.assertTrue(category.default_vm_category)

    def test_import_offerings_with_references(self):
        """Test that importing offerings creates offerings with FK relationships."""
        # Create dependencies
        customer = structure_factories.CustomerFactory(
            uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )
        category = marketplace_factories.CategoryFactory(
            uuid="cccccccc-cccc-cccc-cccc-cccccccccccc"
        )

        offerings_data = [
            {
                "uuid": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "name": "Test Offering",
                "description": "Test offering description",
                "type": "Test.Type",
                "state": 1,
                "customer_uuid": str(customer.uuid),
                "category_uuid": str(category.uuid),
                "shared": True,
                "billable": False,
                "attributes": {"key": "value"},
                "options": {"option": "value"},
                "resource_options": {},
                "plugin_options": {},
            }
        ]

        data = {"offerings": offerings_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify offering created with correct relationships
        self.assertEqual(Offering.objects.count(), 1)

        offering = Offering.objects.get(uuid="dddddddd-dddd-dddd-dddd-dddddddddddd")
        self.assertEqual(offering.name, "Test Offering")
        self.assertEqual(offering.customer, customer)
        self.assertEqual(offering.category, category)
        self.assertEqual(offering.type, "Test.Type")
        self.assertTrue(offering.shared)
        self.assertFalse(offering.billable)
        self.assertEqual(offering.attributes, {"key": "value"})

    # Update Mode Tests

    def test_update_existing_users_when_update_flag_set(self):
        """Test that --update flag updates existing users instead of skipping."""
        # Create existing user
        existing_user = structure_factories.UserFactory(
            uuid="11111111-1111-1111-1111-111111111111",
            username="oldusername",
            email="old@example.com",
            full_name="Old Name",
        )

        users_data = [
            {
                "uuid": str(existing_user.uuid),
                "username": "newusername",
                "email": "new@example.com",
                "full_name": "New Name",
                "organization": "New Org",
                "is_staff": False,
                "is_support": False,
                "is_active": True,
            }
        ]

        data = {"users": users_data}
        self._create_test_json(data)

        output = self._call_import_command("-i", self.test_file_path, "--update")

        # Verify user updated
        existing_user.refresh_from_db()
        self.assertEqual(existing_user.username, "newusername")
        self.assertEqual(existing_user.email, "new@example.com")
        self.assertEqual(existing_user.full_name, "New Name")
        self.assertEqual(existing_user.organization, "New Org")

        # Verify stats show update
        self.assertIn("Updated: 1", output)

    def test_skip_existing_objects_without_update_flag(self):
        """Test that existing objects are skipped without --update flag."""
        # Create existing customer
        existing_customer = structure_factories.CustomerFactory(
            uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", name="Original Name"
        )

        customers_data = [
            {
                "uuid": str(existing_customer.uuid),
                "name": "New Name",
                "email": "new@example.com",
            }
        ]

        data = {"customers": customers_data}
        self._create_test_json(data)

        output = self._call_import_command("-i", self.test_file_path)

        # Verify customer NOT updated
        existing_customer.refresh_from_db()
        self.assertEqual(existing_customer.name, "Original Name")

        # Verify stats show skipped
        self.assertIn("Skipped: 1", output)

    # Skip Options Tests

    def test_skip_users_option(self):
        """Test that --skip-users flag skips user import."""
        users_data = [
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "username": "testuser",
                "email": "test@example.com",
                "is_staff": False,
                "is_support": False,
                "is_active": True,
            }
        ]

        data = {"users": users_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path, "--skip-users")

        # Verify no users created
        self.assertEqual(User.objects.count(), 0)

    def test_skip_roles_option(self):
        """Test that --skip-roles flag skips roles and role permissions import."""
        content_type = ContentType.objects.get_for_model(Customer)

        roles_data = [
            {
                "name": "TestRole",
                "description": "Test role",
                "content_type": f"{content_type.app_label}.{content_type.model}",
                "is_system_role": False,
                "is_active": True,
            }
        ]

        data = {"roles": roles_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path, "--skip-roles")

        # Verify no roles created
        self.assertFalse(Role.objects.filter(name="TestRole").exists())

    # Dry Run Tests

    def test_dry_run_mode_makes_no_changes(self):
        """Test that --dry-run flag prevents any database changes."""
        users_data = [
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "username": "testuser",
                "email": "test@example.com",
                "is_staff": False,
                "is_support": False,
                "is_active": True,
            }
        ]

        customers_data = [
            {"uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "name": "Test Customer"}
        ]

        data = {"users": users_data, "customers": customers_data}
        self._create_test_json(data)

        output = self._call_import_command("-i", self.test_file_path, "--dry-run")

        # Verify no objects created
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Customer.objects.count(), 0)

        # Verify dry run message in output
        self.assertIn("DRY RUN MODE", output)
        self.assertIn("Dry run completed - no changes made", output)

    def test_dry_run_shows_correct_statistics(self):
        """Test that dry run mode shows accurate statistics of what would be imported."""
        users_data = [
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "username": "testuser",
                "email": "test@example.com",
                "is_staff": False,
                "is_support": False,
                "is_active": True,
            }
        ]

        data = {"users": users_data}
        self._create_test_json(data)

        output = self._call_import_command("-i", self.test_file_path, "--dry-run")

        # Verify stats show what would be created
        self.assertIn("Users:", output)
        self.assertIn("Created: 1", output)

    # Role & Permission Tests

    def test_import_roles_with_content_types(self):
        """Test importing roles with content type references."""
        content_type = ContentType.objects.get_for_model(Customer)

        roles_data = [
            {
                "name": "CustomerManager",
                "description": "Manages customers",
                "content_type": f"{content_type.app_label}.{content_type.model}",
                "is_system_role": True,
                "is_active": True,
            }
        ]

        data = {"roles": roles_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify role created with correct content type
        role = Role.objects.get(name="CustomerManager")
        self.assertEqual(role.description, "Manages customers")
        self.assertEqual(role.content_type, content_type)
        self.assertTrue(role.is_system_role)
        self.assertTrue(role.is_active)

    def test_import_role_permissions(self):
        """Test importing role permissions."""
        content_type = ContentType.objects.get_for_model(Customer)

        # Create role first
        role = Role.objects.create(
            name="TestRole",
            description="Test",
            content_type=content_type,
            is_active=True,
        )

        role_permissions_data = [
            {
                "role_name": "TestRole",
                "permission": "view_customer",
            },
            {
                "role_name": "TestRole",
                "permission": "edit_customer",
            },
        ]

        data = {"role_permissions": role_permissions_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify permissions created
        permissions = RolePermission.objects.filter(role=role)
        self.assertEqual(permissions.count(), 2)
        permission_names = list(permissions.values_list("permission", flat=True))
        self.assertIn("view_customer", permission_names)
        self.assertIn("edit_customer", permission_names)

    def test_import_user_roles_with_scopes(self):
        """Test importing user roles with generic FK scopes."""
        # Create dependencies
        user = structure_factories.UserFactory(
            uuid="11111111-1111-1111-1111-111111111111"
        )
        customer = structure_factories.CustomerFactory(
            uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )
        content_type = ContentType.objects.get_for_model(Customer)
        role = Role.objects.create(
            uuid="99999999-9999-9999-9999-999999999999",
            name="TestRole",
            description="Test",
            content_type=content_type,
            is_active=True,
        )

        user_roles_data = [
            {
                "uuid": "88888888-8888-8888-8888-888888888888",
                "user_uuid": str(user.uuid),
                "role_uuid": str(role.uuid),
                "scope_type": f"{content_type.app_label}.{content_type.model}",
                "scope_uuid": str(customer.uuid),
                "is_active": True,
            }
        ]

        data = {"user_roles": user_roles_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify user role created with correct scope
        user_role = UserRole.objects.get(uuid="88888888-8888-8888-8888-888888888888")
        self.assertEqual(user_role.user, user)
        self.assertEqual(user_role.role, role)
        self.assertEqual(user_role.content_type, content_type)
        self.assertEqual(user_role.object_id, customer.id)
        self.assertTrue(user_role.is_active)

    # Error Handling Tests

    def test_missing_input_file_shows_error(self):
        """Test that non-existent input file produces error message."""
        output = self._call_import_command("-i", "/nonexistent/file.json")

        self.assertIn("Input file does not exist", output)

    def test_invalid_json_shows_error(self):
        """Test that malformed JSON produces error message."""
        # Write invalid JSON
        with open(self.test_file_path, "w") as f:
            f.write("{invalid json")

        output = self._call_import_command("-i", self.test_file_path)

        self.assertIn("Invalid JSON file", output)

    def test_missing_uuid_skips_object(self):
        """Test that objects without UUIDs are skipped with error count."""
        users_data = [
            {
                # Missing uuid field
                "username": "testuser",
                "email": "test@example.com",
                "is_staff": False,
                "is_support": False,
                "is_active": True,
            }
        ]

        data = {"users": users_data}
        self._create_test_json(data)

        output = self._call_import_command("-i", self.test_file_path)

        # Verify no users created and error counted
        self.assertEqual(User.objects.count(), 0)
        self.assertIn("Errors: 1", output)

    def test_missing_foreign_key_skips_object(self):
        """Test that objects with missing FK references are skipped."""
        projects_data = [
            {
                "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "name": "Test Project",
                "customer_uuid": "nonexistent-uuid",  # Customer doesn't exist
            }
        ]

        data = {"projects": projects_data}
        self._create_test_json(data)

        output = self._call_import_command("-i", self.test_file_path)

        # Verify project not created
        self.assertEqual(Project.available_objects.count(), 0)
        self.assertIn("customer", output.lower())

    def test_invalid_content_type_skips_role(self):
        """Test that roles with invalid content types are skipped."""
        roles_data = [
            {
                "name": "TestRole",
                "description": "Test",
                "content_type": "invalid.contenttype",  # Invalid content type
                "is_system_role": False,
                "is_active": True,
            }
        ]

        data = {"roles": roles_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify role not created
        self.assertFalse(Role.objects.filter(name="TestRole").exists())

    # Statistics Summary Tests

    def test_statistics_summary_printed(self):
        """Test that import summary statistics are printed correctly."""
        users_data = [
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "username": "user1",
                "email": "user1@example.com",
                "is_staff": False,
                "is_support": False,
                "is_active": True,
            },
            {
                "uuid": "22222222-2222-2222-2222-222222222222",
                "username": "user2",
                "email": "user2@example.com",
                "is_staff": False,
                "is_support": False,
                "is_active": True,
            },
        ]

        data = {"users": users_data}
        self._create_test_json(data)

        output = self._call_import_command("-i", self.test_file_path)

        # Verify summary structure
        self.assertIn("Import Summary:", output)
        self.assertIn("Users:", output)
        self.assertIn("Created: 2", output)
        self.assertIn("Updated: 0", output)
        self.assertIn("Skipped: 0", output)
        self.assertIn("Total Created: 2", output)
        # Errors line only appears when count > 0
        self.assertNotIn("Errors:", output)

    def test_error_count_tracked_correctly(self):
        """Test that errors are tracked and displayed in statistics."""
        users_data = [
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "username": "validuser",
                "email": "valid@example.com",
                "is_staff": False,
                "is_support": False,
                "is_active": True,
            },
            {
                # Missing required email field
                "uuid": "22222222-2222-2222-2222-222222222222",
                "username": "invaliduser",
                "is_staff": False,
                "is_support": False,
                "is_active": True,
            },
        ]

        data = {"users": users_data}
        self._create_test_json(data)

        output = self._call_import_command("-i", self.test_file_path)

        # Verify error count in output
        self.assertIn("Errors: 1", output)

    # Round-trip Test

    def test_export_then_import_preserves_data(self):
        """Test that exporting then importing preserves all data correctly."""
        # Create test data
        user = structure_factories.UserFactory(
            username="exportuser", email="export@example.com", full_name="Export User"
        )
        customer = structure_factories.CustomerFactory(
            name="Export Customer", abbreviation="EC"
        )
        project = structure_factories.ProjectFactory(
            name="Export Project", customer=customer
        )
        category = marketplace_factories.CategoryFactory(title="Export Category")

        # Export data
        export_file = os.path.join(self.temp_dir, "export.json")
        export_output = StringIO()
        call_command("export_structure", "-o", export_file, stdout=export_output)

        # Delete all objects
        User.objects.all().delete()
        Customer.objects.all().delete()
        Category.objects.all().delete()

        # Import data back
        self._call_import_command("-i", export_file)

        # Verify data restored
        restored_user = User.objects.get(uuid=user.uuid)
        self.assertEqual(restored_user.username, "exportuser")
        self.assertEqual(restored_user.email, "export@example.com")

        restored_customer = Customer.objects.get(uuid=customer.uuid)
        self.assertEqual(restored_customer.name, "Export Customer")

        restored_project = Project.available_objects.get(uuid=project.uuid)
        self.assertEqual(restored_project.name, "Export Project")
        self.assertEqual(restored_project.customer.uuid, customer.uuid)

        restored_category = Category.objects.get(uuid=category.uuid)
        self.assertEqual(restored_category.title, "Export Category")

    def test_import_project_service_accounts_creates_new_accounts(self):
        """Test that importing project service accounts creates new account objects."""
        # Create dependencies
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)

        accounts_data = [
            {
                "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "username": "project_account1",
                "email": "project1@example.com",
                "preferred_identifier": "pref_id_1",
                "description": "Test project service account 1",
                "state": 1,
                "project_uuid": project.uuid.hex,
            },
            {
                "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "username": "project_account2",
                "email": "project2@example.com",
                "preferred_identifier": "pref_id_2",
                "description": "Test project service account 2",
                "state": 1,
                "project_uuid": project.uuid.hex,
            },
        ]

        data = {"project_service_accounts": accounts_data}
        self._create_test_json(data)

        from waldur_mastermind.marketplace.models import ProjectServiceAccount

        self._call_import_command("-i", self.test_file_path)

        # Verify accounts created
        self.assertEqual(ProjectServiceAccount.objects.count(), 2)

        account1 = ProjectServiceAccount.objects.get(
            uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )
        self.assertEqual(account1.username, "project_account1")
        self.assertEqual(account1.email, "project1@example.com")
        self.assertEqual(account1.preferred_identifier, "pref_id_1")
        self.assertEqual(account1.description, "Test project service account 1")
        self.assertEqual(account1.project.uuid, project.uuid)

        account2 = ProjectServiceAccount.objects.get(
            uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        )
        self.assertEqual(account2.username, "project_account2")
        self.assertEqual(account2.project.uuid, project.uuid)

    def test_import_customer_service_accounts_creates_new_accounts(self):
        """Test that importing customer service accounts creates new account objects."""
        # Create dependencies
        customer = structure_factories.CustomerFactory()

        accounts_data = [
            {
                "uuid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "username": "customer_account1",
                "email": "customer1@example.com",
                "preferred_identifier": "cust_pref_id_1",
                "description": "Test customer service account 1",
                "state": 1,
                "customer_uuid": customer.uuid.hex,
            },
            {
                "uuid": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "username": "customer_account2",
                "email": "customer2@example.com",
                "preferred_identifier": "cust_pref_id_2",
                "description": "Test customer service account 2",
                "state": 1,
                "customer_uuid": customer.uuid.hex,
            },
        ]

        data = {"customer_service_accounts": accounts_data}
        self._create_test_json(data)

        from waldur_mastermind.marketplace.models import CustomerServiceAccount

        self._call_import_command("-i", self.test_file_path)

        # Verify accounts created
        self.assertEqual(CustomerServiceAccount.objects.count(), 2)

        account1 = CustomerServiceAccount.objects.get(
            uuid="cccccccc-cccc-cccc-cccc-cccccccccccc"
        )
        self.assertEqual(account1.username, "customer_account1")
        self.assertEqual(account1.email, "customer1@example.com")
        self.assertEqual(account1.preferred_identifier, "cust_pref_id_1")
        self.assertEqual(account1.description, "Test customer service account 1")
        self.assertEqual(account1.customer.uuid, customer.uuid)

        account2 = CustomerServiceAccount.objects.get(
            uuid="dddddddd-dddd-dddd-dddd-dddddddddddd"
        )
        self.assertEqual(account2.username, "customer_account2")
        self.assertEqual(account2.customer.uuid, customer.uuid)

    def test_import_course_accounts_creates_new_accounts(self):
        """Test that importing course accounts creates new account objects."""
        # Create dependencies
        user = structure_factories.UserFactory()
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)

        accounts_data = [
            {
                "uuid": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                "email": "course1@example.com",
                "description": "Test course account 1",
                "state": 1,
                "project_uuid": project.uuid.hex,
                "user_uuid": user.uuid.hex,
                "error_message": "",
            },
            {
                "uuid": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                "email": "course2@example.com",
                "description": "Test course account 2",
                "state": 1,
                "project_uuid": project.uuid.hex,
                "user_uuid": user.uuid.hex,
                "error_message": "",
            },
        ]

        data = {"course_accounts": accounts_data}
        self._create_test_json(data)

        from waldur_mastermind.marketplace.models import CourseAccount

        self._call_import_command("-i", self.test_file_path)

        # Verify accounts created
        self.assertEqual(CourseAccount.objects.count(), 2)

        account1 = CourseAccount.objects.get(
            uuid="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        )
        self.assertEqual(account1.email, "course1@example.com")
        self.assertEqual(account1.description, "Test course account 1")
        self.assertEqual(account1.project.uuid, project.uuid)
        self.assertEqual(account1.user.uuid, user.uuid)

        account2 = CourseAccount.objects.get(
            uuid="ffffffff-ffff-ffff-ffff-ffffffffffff"
        )
        self.assertEqual(account2.email, "course2@example.com")
        self.assertEqual(account2.project.uuid, project.uuid)
        self.assertEqual(account2.user.uuid, user.uuid)

    def test_import_plans_creates_new_plans(self):
        """Test that importing plans creates new plan objects."""
        # Create dependencies
        offering = marketplace_factories.OfferingFactory()

        plans_data = [
            {
                "uuid": "aaaaaaaa-bbbb-cccc-dddd-111111111111",
                "offering_uuid": offering.uuid.hex,
                "name": "Basic Plan",
                "description": "Basic plan description",
                "unit_price": "100.50",
                "unit": "month",
                "archived": False,
                "max_amount": 10,
                "article_code": "PLAN-BASIC",
            },
            {
                "uuid": "aaaaaaaa-bbbb-cccc-dddd-222222222222",
                "offering_uuid": offering.uuid.hex,
                "name": "Premium Plan",
                "description": "Premium plan description",
                "unit_price": "250.00",
                "unit": "month",
                "archived": True,
                "max_amount": 5,
                "article_code": "PLAN-PREMIUM",
            },
        ]

        data = {"plans": plans_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify plans created
        self.assertEqual(Plan.objects.count(), 2)

        plan1 = Plan.objects.get(uuid="aaaaaaaa-bbbb-cccc-dddd-111111111111")
        self.assertEqual(plan1.name, "Basic Plan")
        self.assertEqual(plan1.description, "Basic plan description")
        self.assertEqual(float(plan1.unit_price), 100.5)
        self.assertEqual(plan1.unit, "month")
        self.assertFalse(plan1.archived)
        self.assertEqual(plan1.max_amount, 10)
        self.assertEqual(plan1.article_code, "PLAN-BASIC")
        self.assertEqual(plan1.offering.uuid, offering.uuid)

        plan2 = Plan.objects.get(uuid="aaaaaaaa-bbbb-cccc-dddd-222222222222")
        self.assertEqual(plan2.name, "Premium Plan")
        self.assertTrue(plan2.archived)

    def test_import_offering_components_creates_new_components(self):
        """Test that importing offering components creates new component objects."""
        # Create dependencies
        offering = marketplace_factories.OfferingFactory()

        components_data = [
            {
                "uuid": "bbbbbbbb-cccc-dddd-eeee-111111111111",
                "offering_uuid": offering.uuid.hex,
                "type": "cpu",
                "name": "CPU",
                "description": "CPU cores",
                "billing_type": "fixed",
                "measured_unit": "cores",
                "limit_period": "month",
                "limit_amount": 16,
                "article_code": "CPU-001",
            },
            {
                "uuid": "bbbbbbbb-cccc-dddd-eeee-222222222222",
                "offering_uuid": offering.uuid.hex,
                "type": "storage",
                "name": "Storage",
                "description": "Disk storage",
                "billing_type": "usage",
                "measured_unit": "GB",
                "limit_period": None,
                "limit_amount": None,
                "article_code": "STORAGE-001",
            },
        ]

        data = {"offering_components": components_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify components created
        self.assertEqual(OfferingComponent.objects.count(), 2)

        component1 = OfferingComponent.objects.get(
            uuid="bbbbbbbb-cccc-dddd-eeee-111111111111"
        )
        self.assertEqual(component1.type, "cpu")
        self.assertEqual(component1.name, "CPU")
        self.assertEqual(component1.description, "CPU cores")
        self.assertEqual(component1.billing_type, "fixed")
        self.assertEqual(component1.measured_unit, "cores")
        self.assertEqual(component1.limit_period, "month")
        self.assertEqual(component1.limit_amount, 16)
        self.assertEqual(component1.article_code, "CPU-001")
        self.assertEqual(component1.offering.uuid, offering.uuid)

        component2 = OfferingComponent.objects.get(
            uuid="bbbbbbbb-cccc-dddd-eeee-222222222222"
        )
        self.assertEqual(component2.type, "storage")
        self.assertEqual(component2.billing_type, "usage")

    def test_import_plan_components_creates_new_plan_components(self):
        """Test that importing plan components creates new plan component objects."""
        # Create dependencies
        offering = marketplace_factories.OfferingFactory()
        plan = marketplace_factories.PlanFactory(offering=offering)
        component1 = marketplace_factories.OfferingComponentFactory(
            offering=offering, type="cpu", name="CPU"
        )
        component2 = marketplace_factories.OfferingComponentFactory(
            offering=offering, type="ram", name="RAM"
        )

        plan_components_data = [
            {
                "plan_uuid": plan.uuid.hex,
                "component_uuid": component1.uuid.hex,
                "amount": 4,
                "price": "50.25",
                "future_price": "55.00",
            },
            {
                "plan_uuid": plan.uuid.hex,
                "component_uuid": component2.uuid.hex,
                "amount": 8,
                "price": "100.00",
                "future_price": None,
            },
        ]

        data = {"plan_components": plan_components_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify plan components created
        self.assertEqual(PlanComponent.objects.count(), 2)

        pc1 = PlanComponent.objects.get(plan=plan, component=component1)
        self.assertEqual(pc1.amount, 4)
        self.assertEqual(float(pc1.price), 50.25)
        self.assertEqual(float(pc1.future_price), 55.0)

        pc2 = PlanComponent.objects.get(plan=plan, component=component2)
        self.assertEqual(pc2.amount, 8)
        self.assertEqual(float(pc2.price), 100.0)
        self.assertIsNone(pc2.future_price)

    def test_import_resources_creates_new_resources(self):
        """Test that importing resources creates new resource objects."""
        # Create dependencies
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)
        offering = marketplace_factories.OfferingFactory()
        plan = marketplace_factories.PlanFactory(offering=offering)

        resources_data = [
            {
                "uuid": "cccccccc-dddd-eeee-ffff-111111111111",
                "name": "Test Resource 1",
                "state": 2,
                "offering_uuid": offering.uuid.hex,
                "plan_uuid": plan.uuid.hex,
                "project_uuid": project.uuid.hex,
                "attributes": {"key1": "value1"},
                "limits": {"cpu": 2, "ram": 4},
                "options": {"option1": "opt_value1"},
                "backend_id": "backend-123",
                "effective_id": "effective-456",
                "description": "Test resource 1 description",
            },
            {
                "uuid": "cccccccc-dddd-eeee-ffff-222222222222",
                "name": "Test Resource 2",
                "state": 1,
                "offering_uuid": offering.uuid.hex,
                "plan_uuid": None,
                "project_uuid": project.uuid.hex,
                "attributes": {},
                "limits": {},
                "options": {},
                "backend_id": "",
                "effective_id": "",
                "description": "",
            },
        ]

        data = {"resources": resources_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify resources created
        self.assertEqual(Resource.objects.count(), 2)

        resource1 = Resource.objects.get(uuid="cccccccc-dddd-eeee-ffff-111111111111")
        self.assertEqual(resource1.name, "Test Resource 1")
        self.assertEqual(resource1.state, 2)
        self.assertEqual(resource1.offering.uuid, offering.uuid)
        self.assertEqual(resource1.plan.uuid, plan.uuid)
        self.assertEqual(resource1.project.uuid, project.uuid)
        self.assertEqual(resource1.attributes, {"key1": "value1"})
        self.assertEqual(resource1.limits, {"cpu": 2, "ram": 4})
        self.assertEqual(resource1.backend_id, "backend-123")

        resource2 = Resource.objects.get(uuid="cccccccc-dddd-eeee-ffff-222222222222")
        self.assertEqual(resource2.name, "Test Resource 2")
        self.assertIsNone(resource2.plan)

    def test_import_component_usages_creates_new_usages(self):
        """Test that importing component usages creates new usage objects."""
        # Create dependencies
        resource = marketplace_factories.ResourceFactory()
        component1 = marketplace_factories.OfferingComponentFactory(
            offering=resource.offering, type="cpu", name="CPU"
        )
        component2 = marketplace_factories.OfferingComponentFactory(
            offering=resource.offering, type="storage", name="Storage"
        )

        usages_data = [
            {
                "uuid": "dddddddd-eeee-ffff-aaaa-111111111111",
                "resource_uuid": resource.uuid.hex,
                "component_uuid": component1.uuid.hex,
                "usage": "100.50",
                "date": "2024-03-15T10:30:00Z",
                "billing_period": "2024-03-01",
                "recurring": True,
                "description": "CPU usage for March",
            },
            {
                "uuid": "dddddddd-eeee-ffff-aaaa-222222222222",
                "resource_uuid": resource.uuid.hex,
                "component_uuid": component2.uuid.hex,
                "usage": "500.00",
                "date": "2024-03-15T10:30:00Z",
                "billing_period": "2024-03-01",
                "recurring": False,
                "description": "Storage usage for March",
            },
        ]

        data = {"component_usages": usages_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify usages created
        self.assertEqual(ComponentUsage.objects.count(), 2)

        usage1 = ComponentUsage.objects.get(uuid="dddddddd-eeee-ffff-aaaa-111111111111")
        self.assertEqual(usage1.resource.uuid, resource.uuid)
        self.assertEqual(usage1.component.uuid, component1.uuid)
        self.assertEqual(float(usage1.usage), 100.5)
        self.assertTrue(usage1.recurring)
        self.assertEqual(usage1.description, "CPU usage for March")

        usage2 = ComponentUsage.objects.get(uuid="dddddddd-eeee-ffff-aaaa-222222222222")
        self.assertEqual(float(usage2.usage), 500.0)
        self.assertFalse(usage2.recurring)

    def test_import_invoices_creates_new_invoices(self):
        """Test that importing invoices creates new invoice objects."""
        # Create dependencies
        customer = structure_factories.CustomerFactory()

        invoices_data = [
            {
                "uuid": "eeeeeeee-ffff-aaaa-bbbb-111111111111",
                "customer_uuid": customer.uuid.hex,
                "month": 3,
                "year": 2024,
                "state": "created",
                "total_cost": "1000.50",
                "total_price": "1200.60",
                "tax_percent": "20.00",
                "invoice_date": "2024-03-31",
                "created": "2024-03-01",
            },
            {
                "uuid": "eeeeeeee-ffff-aaaa-bbbb-222222222222",
                "customer_uuid": customer.uuid.hex,
                "month": 4,
                "year": 2024,
                "state": "pending",
                "total_cost": "2000.00",
                "total_price": "2400.00",
                "tax_percent": "20.00",
                "invoice_date": None,
                "created": "2024-04-01",
            },
        ]

        data = {"invoices": invoices_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify invoices created
        self.assertEqual(Invoice.objects.count(), 2)

        invoice1 = Invoice.objects.get(uuid="eeeeeeee-ffff-aaaa-bbbb-111111111111")
        self.assertEqual(invoice1.customer.uuid, customer.uuid)
        self.assertEqual(invoice1.month, 3)
        self.assertEqual(invoice1.year, 2024)
        self.assertEqual(invoice1.state, "created")
        self.assertEqual(float(invoice1.total_cost), 1000.5)
        self.assertEqual(float(invoice1.total_price), 1200.6)
        self.assertEqual(float(invoice1.tax_percent), 20.0)
        self.assertIsNotNone(invoice1.invoice_date)

        invoice2 = Invoice.objects.get(uuid="eeeeeeee-ffff-aaaa-bbbb-222222222222")
        self.assertEqual(invoice2.month, 4)
        self.assertEqual(invoice2.state, "pending")

    def test_import_invoice_items_creates_new_items(self):
        """Test that importing invoice items creates new invoice item objects."""
        # Create dependencies
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)
        invoice = invoices_factories.InvoiceFactory(customer=customer)
        resource = marketplace_factories.ResourceFactory(project=project)

        invoice_items_data = [
            {
                "uuid": "ffffffff-aaaa-bbbb-cccc-111111111111",
                "invoice_uuid": invoice.uuid.hex,
                "resource_uuid": resource.uuid.hex,
                "project_uuid": project.uuid.hex,
                "name": "Test Invoice Item 1",
                "quantity": "10.50",
                "measured_unit": "hours",
                "unit_price": "25.00",
                "article_code": "ITEM-001",
                "start": "2024-03-01T00:00:00Z",
                "end": "2024-03-31T23:59:59Z",
            },
            {
                "uuid": "ffffffff-aaaa-bbbb-cccc-222222222222",
                "invoice_uuid": invoice.uuid.hex,
                "resource_uuid": None,
                "project_uuid": None,
                "name": "Test Invoice Item 2",
                "quantity": "5.00",
                "measured_unit": "GB",
                "unit_price": "10.00",
                "article_code": "ITEM-002",
                "start": "2024-03-01T00:00:00Z",
                "end": "2024-03-31T23:59:59Z",
            },
        ]

        data = {"invoice_items": invoice_items_data}
        self._create_test_json(data)

        self._call_import_command("-i", self.test_file_path)

        # Verify invoice items created
        self.assertEqual(InvoiceItem.objects.count(), 2)

        item1 = InvoiceItem.objects.get(uuid="ffffffff-aaaa-bbbb-cccc-111111111111")
        self.assertEqual(item1.invoice.uuid, invoice.uuid)
        self.assertEqual(item1.resource.uuid, resource.uuid)
        self.assertEqual(item1.project.uuid, project.uuid)
        self.assertEqual(item1.name, "Test Invoice Item 1")
        self.assertEqual(float(item1.quantity), 10.5)
        self.assertEqual(item1.measured_unit, "hours")
        self.assertEqual(float(item1.unit_price), 25.0)
        self.assertEqual(item1.article_code, "ITEM-001")
        self.assertIsNotNone(item1.start)
        self.assertIsNotNone(item1.end)

        item2 = InvoiceItem.objects.get(uuid="ffffffff-aaaa-bbbb-cccc-222222222222")
        self.assertEqual(item2.name, "Test Invoice Item 2")
        self.assertIsNone(item2.resource)
        self.assertIsNone(item2.project)

    def test_import_orders_creates_new_orders(self):
        """Test that importing orders creates new order objects."""
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)
        offering = marketplace_factories.OfferingFactory()
        plan = marketplace_factories.PlanFactory(offering=offering)
        old_plan = marketplace_factories.PlanFactory(offering=offering)
        resource = marketplace_factories.ResourceFactory(
            offering=offering, project=project, plan=plan
        )
        created_by = structure_factories.UserFactory()
        consumer_reviewer = structure_factories.UserFactory()
        provider_reviewer = structure_factories.UserFactory()

        orders_data = [
            {
                "uuid": "11111111-2222-3333-4444-555555555555",
                "type": 1,  # CREATE
                "state": 3,  # DONE
                "project_uuid": project.uuid.hex,
                "resource_uuid": resource.uuid.hex,
                "offering_uuid": offering.uuid.hex,
                "plan_uuid": plan.uuid.hex,
                "created_by_uuid": created_by.uuid.hex,
                "old_plan_uuid": old_plan.uuid.hex,
                "consumer_reviewed_by_uuid": consumer_reviewer.uuid.hex,
                "provider_reviewed_by_uuid": provider_reviewer.uuid.hex,
                "consumer_reviewed_at": "2024-01-15T10:30:00",
                "provider_reviewed_at": "2024-01-15T11:00:00",
                "output": "Order completed successfully",
                "callback_url": "https://example.com/callback",
                "termination_comment": "User requested",
                "request_comment": "Please create resource",
                "attributes": {"key": "value"},
                "limits": {"cpu": 4, "ram": 8192},
                "cost": "100.50",
            },
            {
                "uuid": "22222222-3333-4444-5555-666666666666",
                "type": 2,  # UPDATE
                "state": 1,  # PENDING_CONSUMER
                "project_uuid": project.uuid.hex,
                "resource_uuid": resource.uuid.hex,
                "offering_uuid": offering.uuid.hex,
                "plan_uuid": None,
                "created_by_uuid": created_by.uuid.hex,
                "old_plan_uuid": None,
                "consumer_reviewed_by_uuid": None,
                "provider_reviewed_by_uuid": None,
                "consumer_reviewed_at": None,
                "provider_reviewed_at": None,
                "output": "",
                "callback_url": "",
                "termination_comment": "",
                "request_comment": "",
                "attributes": {},
                "limits": {},
                "cost": None,
            },
        ]

        data = {"orders": orders_data}
        self._create_test_json(data)
        self._call_import_command("-i", self.test_file_path)

        # Verify orders created
        self.assertEqual(Order.objects.count(), 2)

        # Verify order1 with all fields
        order1 = Order.objects.get(uuid="11111111-2222-3333-4444-555555555555")
        self.assertEqual(order1.type, 1)
        self.assertEqual(order1.state, 3)
        self.assertEqual(order1.project.uuid, project.uuid)
        self.assertEqual(order1.resource.uuid, resource.uuid)
        self.assertEqual(order1.offering.uuid, offering.uuid)
        self.assertEqual(order1.plan.uuid, plan.uuid)
        self.assertEqual(order1.created_by.uuid, created_by.uuid)
        self.assertEqual(order1.old_plan.uuid, old_plan.uuid)
        self.assertEqual(order1.consumer_reviewed_by.uuid, consumer_reviewer.uuid)
        self.assertEqual(order1.provider_reviewed_by.uuid, provider_reviewer.uuid)
        self.assertIsNotNone(order1.consumer_reviewed_at)
        self.assertIsNotNone(order1.provider_reviewed_at)
        self.assertEqual(order1.output, "Order completed successfully")
        self.assertEqual(order1.callback_url, "https://example.com/callback")
        self.assertEqual(order1.termination_comment, "User requested")
        self.assertEqual(order1.request_comment, "Please create resource")
        self.assertEqual(order1.attributes, {"key": "value"})
        self.assertEqual(order1.limits, {"cpu": 4, "ram": 8192})
        self.assertEqual(float(order1.cost), 100.5)

        # Verify order2 with null optional fields
        order2 = Order.objects.get(uuid="22222222-3333-4444-5555-666666666666")
        self.assertEqual(order2.type, 2)
        self.assertEqual(order2.state, 1)
        self.assertEqual(order2.offering.uuid, offering.uuid)
        self.assertIsNone(order2.plan)
        self.assertIsNone(order2.old_plan)
        self.assertIsNone(order2.consumer_reviewed_by)
        self.assertIsNone(order2.provider_reviewed_by)
        self.assertIsNone(order2.consumer_reviewed_at)
        self.assertIsNone(order2.provider_reviewed_at)
        self.assertEqual(order2.attributes, {})
        self.assertEqual(order2.limits, {})
        self.assertIsNone(order2.cost)
