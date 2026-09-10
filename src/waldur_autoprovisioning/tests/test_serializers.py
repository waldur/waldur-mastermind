from django.test import RequestFactory, TestCase

from waldur_autoprovisioning.serializers import RuleSerializer
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.tests import factories as permission_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


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
        # Touching the classproperty get_or_creates the role. Referring to a
        # system role by name without this is a bet on the roles table not
        # having been emptied by an earlier TransactionTestCase in the same
        # process — a bet that loses in some CI shards.
        self.customer_owner_role = CustomerRole.OWNER

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
        """A rule has to grant something: neither a project nor an organization
        role is not a valid rule."""
        data = self.base_data.copy()

        serializer = RuleSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)
        self.assertIn(
            "Either project_role or customer_role must be provided",
            str(serializer.errors["non_field_errors"]),
        )

    def test_customer_role_alone_is_enough(self):
        """An organization-level rule grants no project role at all."""
        data = self.base_data.copy()
        data["customer_role_name"] = self.customer_owner_role.name
        data["create_project"] = False

        serializer = RuleSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        rule = serializer.save()
        self.assertEqual(rule.customer_role, self.customer_owner_role)
        self.assertIsNone(rule.project_role)
        self.assertFalse(rule.create_project)

    def test_rule_without_project_must_have_customer_role(self):
        data = self.base_data.copy()
        data["project_role_name"] = "PROJECT.ADMIN"
        data["create_project"] = False

        serializer = RuleSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn(
            "must specify a customer_role",
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

    def test_update_without_a_role_keeps_the_existing_one(self):
        """An update that simply omits the role fields leaves them alone; it is
        clearing *both* roles that is refused, since the rule would grant
        nothing."""
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
        self.assertTrue(update_serializer.is_valid(), update_serializer.errors)
        updated = update_serializer.save()
        self.assertEqual(updated.project_role, self.project_admin_role)

        # Explicitly clearing the only role the rule has is refused.
        clear_data = dict(update_data, project_role_name=None)
        clear_serializer = RuleSerializer(rule, data=clear_data)
        self.assertFalse(clear_serializer.is_valid())
        self.assertIn(
            "Either project_role or customer_role must be provided",
            str(clear_serializer.errors["non_field_errors"]),
        )

    def test_case_sensitive_role_name_lookup(self):
        """Test that role name lookup is case sensitive."""
        data = self.base_data.copy()
        data["project_role_name"] = "project.admin"  # lowercase

        serializer = RuleSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)
        self.assertIn("does not exist", str(serializer.errors["non_field_errors"]))


class RuleSerializerPlanFieldTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.request = self.factory.get("/")
        self.customer = structure_factories.CustomerFactory()
        self.plan = marketplace_factories.PlanFactory()
        self.base_data = {
            "name": "test_rule",
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "user_email_patterns": [".*@example.com"],
            "user_affiliations": ["org"],
            "project_role": permission_factories.RoleFactory.get_url(ProjectRole.ADMIN),
        }

    def test_public_plan_is_accepted(self):
        data = self.base_data.copy()
        data["plan"] = marketplace_factories.PlanFactory.get_public_url(self.plan)

        serializer = RuleSerializer(data=data, context={"request": self.request})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        rule = serializer.save()
        self.assertEqual(rule.plan, self.plan)

    def test_private_plan_is_not_accepted(self):
        data = self.base_data.copy()
        data["plan"] = marketplace_factories.PlanFactory.get_url(self.plan)

        serializer = RuleSerializer(data=data, context={"request": self.request})
        self.assertFalse(serializer.is_valid())


class RuleSerializerNoCustomerTest(TestCase):
    def setUp(self):
        self.project_admin = ProjectRole.ADMIN
        self.valid_data = {
            "name": "test_rule",
            "user_email_patterns": [".*@example.com", "test@.*"],
            "user_affiliations": ["staff"],
            "project_role_name": "PROJECT.ADMIN",
        }

    def test_rule_creation_without_customer_is_invalid_if_flag_false(self):
        serializer = RuleSerializer(data=self.valid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)
        self.assertIn(
            "Either customer must be specified or use_user_organization_as_customer_name must be true.",
            str(serializer.errors["non_field_errors"]),
        )

    def test_rule_creation_without_customer_is_valid_if_flag_true(self):
        data = self.valid_data.copy()
        data["use_user_organization_as_customer_name"] = True
        serializer = RuleSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
