from unittest import mock
from unittest.mock import call, patch

from django.test import TestCase

from waldur_autoprovisioning import handlers, models
from waldur_autoprovisioning.tests import factories as autoprovisioning_factories
from waldur_core.core.models import User
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

        project = handlers.get_or_create_project(self.rule, self.user)

        self.assertIsNotNone(project)
        self.assertTrue(project.has_user(self.user, ProjectRole.MANAGER))
        self.assertFalse(project.has_user(self.user, ProjectRole.ADMIN))
