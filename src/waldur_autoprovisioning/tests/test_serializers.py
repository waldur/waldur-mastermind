from django.test import RequestFactory, TestCase

from waldur_autoprovisioning.serializers import RuleSerializer
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.tests import factories as permission_factories
from waldur_core.structure.tests import factories as structure_factories


class RuleSerializerTest(TestCase):
    def setUp(self):
        self.project_admin = ProjectRole.ADMIN
        self.valid_data = {
            "name": "test_rule",
            "customer": structure_factories.CustomerFactory.get_url(),
            "user_email_patterns": [".*@example.com", "test@.*"],
            "user_affiliations": ["staff"],
            "project_role_name": "PROJECT.ADMIN",
        }

    def test_valid_regex_patterns_accepted(self):
        serializer = RuleSerializer(data=self.valid_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_invalid_regex_patterns_rejected(self):
        invalid_data = self.valid_data.copy()
        invalid_data["user_email_patterns"] = [
            "*invalid",
            ".+@example.com",
            "+alsoinvalid",
        ]

        serializer = RuleSerializer(data=invalid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("user_email_patterns", serializer.errors)
        self.assertIn(
            "Invalid regex patterns", str(serializer.errors["user_email_patterns"])
        )

    def test_empty_patterns_accepted(self):
        empty_data = self.valid_data.copy()
        empty_data["user_email_patterns"] = []

        serializer = RuleSerializer(data=empty_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_none_patterns_accepted(self):
        none_data = self.valid_data.copy()
        del none_data["user_email_patterns"]

        serializer = RuleSerializer(data=none_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_mixed_valid_invalid_patterns_rejected(self):
        mixed_data = self.valid_data.copy()
        mixed_data["user_email_patterns"] = [".*@example.com", "*invalid", "test@.*"]

        serializer = RuleSerializer(data=mixed_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("user_email_patterns", serializer.errors)
        self.assertIn("*invalid", str(serializer.errors["user_email_patterns"]))

    def test_non_string_patterns_rejected(self):
        invalid_data = self.valid_data.copy()
        invalid_data["user_email_patterns"] = [".*@example.com", 123, None]

        serializer = RuleSerializer(data=invalid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("user_email_patterns", serializer.errors)


class RuleSerializerProjectRoleTest(TestCase):
    """Test cases for project role functionality in RuleSerializer."""

    def setUp(self):
        self.factory = RequestFactory()
        self.request = self.factory.get("/")
        self.customer = structure_factories.CustomerFactory()
        self.base_data = {
            "name": "test_rule",
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "user_email_patterns": [".*@example.com"],
            "user_affiliations": ["org"],
        }
        self.project_admin_role = ProjectRole.ADMIN
        self.project_manager_role = ProjectRole.MANAGER

    def test_project_role_description_exposed(self):
        """Test that project_role_description is properly exposed."""
        data = self.base_data.copy()
        data["project_role"] = permission_factories.RoleFactory.get_url(
            self.project_admin_role
        )

        serializer = RuleSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

        # Check the serialized representation includes description
        rule = serializer.save()
        read_serializer = RuleSerializer(rule, context={"request": self.request})
        self.assertEqual(
            read_serializer.data["project_role_description"],
            self.project_admin_role.description,
        )

    def test_project_role_assignment_by_uuid(self):
        """Test that project_role can be assigned by UUID."""
        data = self.base_data.copy()
        data["project_role"] = permission_factories.RoleFactory.get_url(
            self.project_admin_role
        )

        serializer = RuleSerializer(data=data)
        self.assertTrue(serializer.is_valid())

        rule = serializer.save()
        self.assertEqual(rule.project_role, self.project_admin_role)

    def test_project_role_assignment_by_name(self):
        """Test that project_role can be assigned by name using project_role_name."""
        data = self.base_data.copy()
        data["project_role_name"] = "PROJECT.ADMIN"

        serializer = RuleSerializer(data=data)
        self.assertTrue(serializer.is_valid())

        rule = serializer.save()
        self.assertEqual(rule.project_role, self.project_admin_role)

    def test_project_role_name_lookup_different_role(self):
        """Test project_role_name lookup works with different roles."""
        data = self.base_data.copy()
        data["project_role_name"] = "PROJECT.MANAGER"

        serializer = RuleSerializer(data=data)
        self.assertTrue(serializer.is_valid())

        rule = serializer.save()
        self.assertEqual(rule.project_role, self.project_manager_role)

    def test_project_role_name_nonexistent_role_error(self):
        """Test error when project_role_name refers to non-existent role."""
        data = self.base_data.copy()
        data["project_role_name"] = "PROJECT.NONEXISTENT"

        serializer = RuleSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)
        self.assertIn("does not exist", str(serializer.errors["non_field_errors"]))

    def test_mutual_exclusivity_both_provided_error(self):
        """Test error when both project_role and project_role_name are provided."""
        data = self.base_data.copy()
        data["project_role"] = permission_factories.RoleFactory.get_url(
            self.project_admin_role
        )
        data["project_role_name"] = "PROJECT.MANAGER"

        serializer = RuleSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)
        self.assertIn("Cannot specify both", str(serializer.errors["non_field_errors"]))

    def test_neither_provided_is_invalid(self):
        """Test that providing neither project_role nor project_role_name is invalid when creating."""
        data = self.base_data.copy()

        serializer = RuleSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)
        self.assertIn(
            "Either project_role or project_role_name must be provided",
            str(serializer.errors["non_field_errors"]),
        )

    def test_project_role_name_null_is_invalid(self):
        """Test that project_role_name cannot be null."""
        data = self.base_data.copy()
        data["project_role_name"] = None

        serializer = RuleSerializer(data=data)
        self.assertFalse(serializer.is_valid())

    def test_project_role_name_empty_string_is_invalid(self):
        """Test that empty string for project_role_name is valid."""
        data = self.base_data.copy()
        data["project_role_name"] = ""

        serializer = RuleSerializer(data=data)
        self.assertFalse(serializer.is_valid())

    def test_update_rule_with_project_role_name(self):
        """Test updating an existing rule using project_role_name."""
        # Create initial rule with one role
        initial_data = self.base_data.copy()
        initial_data["project_role"] = permission_factories.RoleFactory.get_url(
            self.project_admin_role
        )

        serializer = RuleSerializer(data=initial_data)
        self.assertTrue(serializer.is_valid())
        rule = serializer.save()

        # Update with different role using project_role_name
        update_data = {
            "name": "some name",
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "user_email_patterns": [".*@example.com"],
            "user_affiliations": ["staff"],
            "project_role_name": "PROJECT.MANAGER",
        }

        update_serializer = RuleSerializer(rule, data=update_data)
        self.assertTrue(update_serializer.is_valid(), update_serializer.errors)
        updated_rule = update_serializer.save()

        self.assertEqual(updated_rule.project_role, self.project_manager_role)

    def test_update_rule_cannot_clear_role(self):
        """Test that updating a rule cannot clear the project role (role is always required)."""
        # Create initial rule with a role
        initial_data = self.base_data.copy()
        initial_data["project_role"] = permission_factories.RoleFactory.get_url(
            self.project_admin_role
        )

        serializer = RuleSerializer(data=initial_data)
        self.assertTrue(serializer.is_valid())
        rule = serializer.save()

        # Attempt to update without providing a role (should fail)
        update_data = {
            "name": "some name",
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "user_email_patterns": [".*@example.com"],
            "user_affiliations": ["staff"],
        }

        update_serializer = RuleSerializer(rule, data=update_data)
        self.assertFalse(update_serializer.is_valid())
        self.assertIn("non_field_errors", update_serializer.errors)
        self.assertIn(
            "Either project_role or project_role_name must be provided",
            str(update_serializer.errors["non_field_errors"]),
        )

    def test_case_sensitive_role_name_lookup(self):
        """Test that role name lookup is case sensitive."""
        data = self.base_data.copy()
        data["project_role_name"] = "project.admin"  # lowercase

        serializer = RuleSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)
        self.assertIn("does not exist", str(serializer.errors["non_field_errors"]))
