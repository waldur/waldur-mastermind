from unittest import mock
from unittest.mock import call, patch

from django.test import TestCase

from waldur_autoprovisioning import handlers, models
from waldur_autoprovisioning.tests import factories as autoprovisioning_factories
from waldur_core.core.models import User
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BASIC_OFFERING as MARKETPLACE_BASIC
from waldur_mastermind.marketplace.enums import OPENSTACK_TENANT_OFFERING
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class HandleNewUserTest(TestCase):
    def setUp(self):
        self.plan_1 = marketplace_factories.PlanFactory()
        self.plan_1.offering.type = MARKETPLACE_BASIC
        self.plan_1.offering.save()

        self.rule_1 = autoprovisioning_factories.RuleFactory(
            plan=self.plan_1, user_email_patterns=[".+@example.com"]
        )

        self.plan_2 = marketplace_factories.PlanFactory()
        self.plan_2.offering.type = OPENSTACK_TENANT_OFFERING
        self.plan_2.offering.save()

        self.rule_2 = autoprovisioning_factories.RuleFactory(
            plan=self.plan_2,
            user_email_patterns=[".+@example.com"],
            plan_limits={"vcpu": 4, "ram": 8192, "storage": 100},
        )

    def _verify_creating_order(self, plan, user, mock_process_order):
        self.assertTrue(
            marketplace_models.Order.objects.filter(
                created_by=user, offering=plan.offering
            ).exists()
        )
        order = marketplace_models.Order.objects.get(
            created_by=user, offering=plan.offering
        )

        mock_process_order.assert_has_calls(
            [
                call(order, user),
            ],
            any_order=True,
        )

        self.assertTrue(
            marketplace_models.Resource.objects.filter(
                offering=order.offering
            ).exists(),
            "Resource should be created for the order.",
        )
        resource = marketplace_models.Resource.objects.filter(
            offering=order.offering
        ).get()
        return resource

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_handler(self, mock_process_order: mock.Mock):
        user = User.objects.create(username="testuser", email="test@example.com")
        self.assertEqual(mock_process_order.call_count, 2)

        self._verify_creating_order(self.plan_1, user, mock_process_order)

        resource = self._verify_creating_order(self.plan_2, user, mock_process_order)
        self.assertEqual(resource.limits, self.rule_2.plan_limits)
        self.assertTrue(
            structure_models.Project.available_objects.filter(
                name=user.username, customer=self.rule_1.customer
            ).exists()
        )
        project = structure_models.Project.available_objects.filter(
            name=user.username, customer=self.rule_1.customer
        ).get()
        self.assertFalse(project.is_removed)
        self.assertTrue(project.has_user(user, ProjectRole.ADMIN))


class CreateProjectWithoutResourcesTest(TestCase):
    def setUp(self):
        self.rule = autoprovisioning_factories.RuleFactory(plan=None)
        self.rule.user_email_patterns = [".+@example.com"]
        self.rule.save()

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_create_project_without_resource(self, mock_process_order):
        user = User.objects.create(username="testuser", email="test@example.com")
        self.assertEqual(mock_process_order.call_count, 0)
        project = structure_models.Project.available_objects.filter(
            name=user.username, customer=self.rule.customer
        ).get()
        self.assertFalse(project.is_removed)
        self.assertTrue(project.has_user(user, ProjectRole.ADMIN))

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_not_admin_project_role(self, mock_process_order):
        self.rule.project_role = ProjectRole.MANAGER
        self.rule.save()
        user = User.objects.create(username="testuser", email="test@example.com")
        self.assertEqual(mock_process_order.call_count, 0)
        project = structure_models.Project.available_objects.filter(
            name=user.username, customer=self.rule.customer
        ).get()
        self.assertFalse(project.is_removed)
        self.assertTrue(project.has_user(user, ProjectRole.MANAGER))

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_create_project_with_name_template(self, mock_process_order):
        self.rule.project_name_template = "{username}_auto_project"
        self.rule.save()
        user = User.objects.create(username="testuser", email="test@example.com")
        self.assertEqual(mock_process_order.call_count, 0)
        project = structure_models.Project.available_objects.filter(
            name="testuser_auto_project", customer=self.rule.customer
        ).get()
        self.assertFalse(project.is_removed)
        self.assertTrue(project.has_user(user, ProjectRole.ADMIN))


class InvalidRegexPatternsTest(TestCase):
    def test_is_pattern_match_with_invalid_regex(self):
        self.assertFalse(models.Rule._is_pattern_match("*invalid", "test@example.com"))
        self.assertFalse(models.Rule._is_pattern_match("+invalid", "test@example.com"))
        self.assertFalse(models.Rule._is_pattern_match("?invalid", "test@example.com"))
        self.assertFalse(models.Rule._is_pattern_match("", "test@example.com"))
        self.assertFalse(models.Rule._is_pattern_match(None, "test@example.com"))
        self.assertFalse(models.Rule._is_pattern_match(123, "test@example.com"))

    def test_is_pattern_match_with_valid_regex(self):
        self.assertTrue(
            models.Rule._is_pattern_match(".*@example.com", "test@example.com")
        )
        self.assertFalse(
            models.Rule._is_pattern_match(".*@other.com", "test@example.com")
        )
        self.assertTrue(models.Rule._is_pattern_match("test.*", "test@example.com"))

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_get_rules_handles_invalid_regex_patterns(self, mock_process_order):
        rule = autoprovisioning_factories.RuleFactory()
        rule.user_email_patterns = ["*invalid", ".+@example.com", "+alsoinvalid"]
        rule.save()

        user = User.objects.create(username="testuser", email="test@example.com")

        rules = models.Rule.get_objects_by_user_patterns(user)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0], rule)


class GetOrCreateProjectWithTemplateTest(TestCase):
    def setUp(self):
        self.rule = autoprovisioning_factories.RuleFactory(
            project_name_template="{username}_custom_workspace"
        )
        self.user = User.objects.create(
            username="test_user",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_get_or_create_project_uses_template(self, mock_process_order):
        project = handlers.get_or_create_project(self.rule, self.user)

        self.assertIsNotNone(project)
        self.assertEqual(project.name, "test_user_custom_workspace")
        self.assertEqual(project.customer, self.rule.customer)
        self.assertTrue(project.has_user(self.user, ProjectRole.ADMIN))

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_get_or_create_project_without_template_uses_username(
        self, mock_process_order
    ):
        self.rule.project_name_template = ""
        self.rule.save()

        project = handlers.get_or_create_project(self.rule, self.user)

        self.assertIsNotNone(project)
        self.assertEqual(project.name, "test_user")
        self.assertEqual(project.customer, self.rule.customer)
        self.assertTrue(project.has_user(self.user, ProjectRole.ADMIN))

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_get_or_create_project_returns_existing_project(self, mock_process_order):
        # Create project first time
        project1 = handlers.get_or_create_project(self.rule, self.user)

        # Call again should return same project
        project2 = handlers.get_or_create_project(self.rule, self.user)

        self.assertEqual(project1.id, project2.id)
        self.assertEqual(project1.name, "test_user_custom_workspace")

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_get_or_create_project_with_custom_role(self, mock_process_order):
        self.rule.project_role = ProjectRole.MANAGER
        self.rule.save()

        # Create a new user after setting project_role to MANAGER
        # This avoids the signal handler creating a project with ADMIN role
        # when the user is created in setUp (before project_role is set)
        new_user = User.objects.create(
            username="custom_role_user",
            email="custom_role@example.com",
            first_name="Custom",
            last_name="Role",
        )

        project = handlers.get_or_create_project(self.rule, new_user)

        self.assertIsNotNone(project)
        self.assertTrue(project.has_user(new_user, ProjectRole.MANAGER))
        self.assertFalse(project.has_user(new_user, ProjectRole.ADMIN))


class ProjectProvisionByOrganizationTest(TestCase):
    def setUp(self):
        self.organization_name = "OrgFromIdP"
        self.customer = structure_models.Customer.objects.create(
            name=self.organization_name
        )
        self.plan = marketplace_factories.PlanFactory()
        self.plan.offering.type = MARKETPLACE_BASIC
        self.plan.offering.save()
        self.rule = autoprovisioning_factories.RuleFactory(
            plan=self.plan, user_email_patterns=[".+@example.com"], customer=None
        )
        # Enable taking customer from user's organization
        self.rule.use_user_organization_as_customer_name = True
        self.rule.save()

    @override_waldur_core_settings(
        PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS=["PROTECTED"]
    )
    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_project_created_for_user_organization(self, mock_process_order):
        user = User.objects.create(
            username="orguser",
            email="orguser@example.com",
            organization=self.organization_name,
            registration_method="PROTECTED",
        )
        project = structure_models.Project.available_objects.filter(
            name=user.username, customer=self.customer
        ).first()
        self.assertIsNotNone(
            project, "Project should be created for user's organization"
        )
        self.assertTrue(
            project.has_user(user, ProjectRole.ADMIN),
            "User should have ADMIN role in the project",
        )


class GetOrCreateProjectPolicyTest(TestCase):
    """The org-scoping policy is respected when auto-provisioning grants a role,
    on both the existing-project and the newly-created-project branches."""

    def _make_user(self):
        # Email does not match the rule pattern, so the post_save signal does not
        # auto-provision — the handler is exercised directly instead.
        return User.objects.create(username="npuser", email="npuser@nowhere.test")

    def _concealed_rule(self):
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.permissions.models import CustomerRoleConcealment
        from waldur_core.structure.models import Customer

        rule = autoprovisioning_factories.RuleFactory(
            project_role=ProjectRole.ADMIN,
            user_email_patterns=[".+@example.com"],
        )
        CustomerRoleConcealment.objects.create(
            role=ProjectRole.ADMIN,
            content_type=ContentType.objects.get_for_model(Customer),
            object_id=rule.customer.id,
        )
        return rule

    def test_concealed_role_skipped_when_creating_project(self):
        rule = self._concealed_rule()
        user = self._make_user()
        # No project exists yet -> exercises the Project.DoesNotExist branch.
        project = handlers.get_or_create_project(rule, user)
        self.assertIsNotNone(project)
        self.assertFalse(project.has_user(user, ProjectRole.ADMIN))

    def test_concealed_role_skipped_for_existing_project(self):
        rule = self._concealed_rule()
        user = self._make_user()
        project = structure_models.Project.available_objects.create(
            name=rule.resolve_project_name(user), customer=rule.customer
        )
        result = handlers.get_or_create_project(rule, user)
        self.assertEqual(result.pk, project.pk)
        self.assertFalse(project.has_user(user, ProjectRole.ADMIN))
