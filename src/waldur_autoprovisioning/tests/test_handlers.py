from unittest.mock import call, patch

from django.test import TestCase

from waldur_autoprovisioning.tests import factories as autoprovisioning_factories
from waldur_core.core.models import User
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import PLUGIN_NAME as MARKETPLACE_BASIC
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack import TENANT_TYPE


class HandleNewUserTest(TestCase):
    def setUp(self):
        self.rule_plan_1 = autoprovisioning_factories.RulePlansFactory()
        self.rule = self.rule_plan_1.rule
        self.rule.user_email_patterns = [".+@example.com"]
        self.rule.save()
        self.plan_1 = self.rule_plan_1.plan
        self.plan_1.offering.type = MARKETPLACE_BASIC
        self.plan_1.offering.save()

        self.rule_plan_2 = autoprovisioning_factories.RulePlansFactory(
            rule=self.rule, limits={"vcpu": 4, "ram": 8192, "storage": 100}
        )
        self.plan_2 = self.rule_plan_2.plan
        self.plan_2.offering.type = TENANT_TYPE
        self.plan_2.offering.save()

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
    def test_handler(self, mock_process_order):
        user = User.objects.create(username="testuser", email="test@example.com")
        self.assertEqual(mock_process_order.call_count, 2)

        self._verify_creating_order(self.plan_1, user, mock_process_order)

        resource = self._verify_creating_order(self.plan_2, user, mock_process_order)
        self.assertEqual(resource.limits, self.rule_plan_2.limits)
        self.assertTrue(
            structure_models.Project.available_objects.filter(
                name=user.username, customer=self.rule.customer
            ).exists()
        )
        project = structure_models.Project.available_objects.filter(
            name=user.username, customer=self.rule.customer
        ).get()
        self.assertFalse(project.is_removed)
        self.assertTrue(project.has_user(user, ProjectRole.ADMIN))
