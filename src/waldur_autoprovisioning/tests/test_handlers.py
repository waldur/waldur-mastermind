from unittest import mock
from unittest.mock import call, patch

from django.test import TestCase

from waldur_autoprovisioning import handlers
from waldur_autoprovisioning.tests import factories as autoprovisioning_factories
from waldur_core.core.models import User
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import PLUGIN_NAME as MARKETPLACE_BASIC
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import TENANT_TYPE


class HandleNewUserTest(TestCase):
    def setUp(self):
        self.plan_1 = marketplace_factories.PlanFactory()
        self.plan_1.offering.type = MARKETPLACE_BASIC
        self.plan_1.offering.save()

        self.rule_1 = autoprovisioning_factories.RuleFactory(
            plan=self.plan_1, user_email_patterns=[".+@example.com"]
        )

        self.plan_2 = marketplace_factories.PlanFactory()
        self.plan_2.offering.type = TENANT_TYPE
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


class InvalidRegexPatternsTest(TestCase):
    def test_is_pattern_match_with_invalid_regex(self):
        self.assertFalse(handlers._is_pattern_match("*invalid", "test@example.com"))
        self.assertFalse(handlers._is_pattern_match("+invalid", "test@example.com"))
        self.assertFalse(handlers._is_pattern_match("?invalid", "test@example.com"))
        self.assertFalse(handlers._is_pattern_match("", "test@example.com"))
        self.assertFalse(handlers._is_pattern_match(None, "test@example.com"))
        self.assertFalse(handlers._is_pattern_match(123, "test@example.com"))

    def test_is_pattern_match_with_valid_regex(self):
        self.assertTrue(
            handlers._is_pattern_match(".*@example.com", "test@example.com")
        )
        self.assertFalse(handlers._is_pattern_match(".*@other.com", "test@example.com"))
        self.assertTrue(handlers._is_pattern_match("test.*", "test@example.com"))

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_get_rules_handles_invalid_regex_patterns(self, mock_process_order):
        rule = autoprovisioning_factories.RuleFactory()
        rule.user_email_patterns = ["*invalid", ".+@example.com", "+alsoinvalid"]
        rule.save()

        user = User.objects.create(username="testuser", email="test@example.com")

        rules = handlers.get_rules(user)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0], rule)
