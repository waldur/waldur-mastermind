from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests.factories import OFFERING_OPTIONS
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.marketplace_support import utils as marketplace_support_utils
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests import factories as support_factories
from waldur_mastermind.support.tests.base import BaseTest


class OfferingTemplateCreateTest(test.APITransactionTestCase):
    def test_offering_template_is_created_for_valid_type(self):
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        offering.refresh_from_db()
        template = support_models.OfferingTemplate.objects.get(name=offering.name)
        self.assertTrue(offering.scope, template)

    def test_when_plan_price_is_updated_offering_template_is_synchronized(self):
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        offering.refresh_from_db()
        plan = marketplace_factories.PlanFactory(offering=offering, unit_price=100)
        plan.refresh_from_db()
        self.assertTrue(plan.scope.unit_price, 100)

        plan.unit_price += 1
        plan.save()
        plan.scope.refresh_from_db()
        self.assertTrue(plan.scope.unit_price, 1001)

    def test_offering_template_is_not_created_for_invalid_type(self):
        offering = marketplace_factories.OfferingFactory()
        offering.refresh_from_db()
        self.assertIsNone(offering.scope)


class OfferingTemplateUpdateTest(test.APITransactionTestCase):
    def test_offering_template_is_updated(self):
        # Arrange
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        offering.refresh_from_db()
        template = support_models.OfferingTemplate.objects.get(name=offering.name)

        # Act
        offering.options = OFFERING_OPTIONS
        offering.save()

        # Assert
        template.refresh_from_db()
        self.assertEqual(template.config, offering.options)


class SupportOfferingTest(BaseTest):
    def setUp(self):
        super(SupportOfferingTest, self).setUp()

        self.success_issue_status = 'Completed'
        support_factories.IssueStatusFactory(
            name=self.success_issue_status,
            type=support_models.IssueStatus.Types.RESOLVED,
        )

        self.fail_issue_status = 'Cancelled'
        support_factories.IssueStatusFactory(
            name=self.fail_issue_status, type=support_models.IssueStatus.Types.CANCELED
        )

        self.offering = support_factories.OfferingFactory()
        resource = marketplace_factories.ResourceFactory(scope=self.offering)
        self.order_item = marketplace_factories.OrderItemFactory(resource=resource)
        self.order_item.set_state_executing()
        self.order_item.save()

        self.order_item.order.state = marketplace_models.Order.States.EXECUTING
        self.order_item.order.save()

    def test_offering_set_state_ok_if_issue_resolved(self):
        self.offering.issue.status = self.success_issue_status
        self.offering.issue.save()

        self.order_item.refresh_from_db()
        self.assertEqual(self.order_item.state, self.order_item.States.DONE)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.state, support_models.Offering.States.OK)

    def test_offering_set_state_terminated_if_issue_canceled(self):
        self.offering.issue.status = self.fail_issue_status
        self.offering.issue.save()

        self.order_item.refresh_from_db()
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.ERRED
        )
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.state, support_models.Offering.States.TERMINATED)


class SupportOfferingResourceTest(BaseTest):
    def test_create_missing_support_offerings(self):
        offering = support_factories.OfferingFactory()
        category = marketplace_factories.CategoryFactory()
        customer = structure_factories.CustomerFactory()

        marketplace_support_utils.init_offerings_and_resources(category, customer)
        self.assertTrue(
            marketplace_models.Resource.objects.filter(
                scope=offering, project=offering.project
            ).exists()
        )

    def test_filter_when_creating_missing_support_offerings(self):
        offering = support_factories.OfferingFactory()
        category = marketplace_factories.CategoryFactory()
        customer = structure_factories.CustomerFactory()
        new_project = structure_factories.ProjectFactory()
        marketplace_factories.ResourceFactory(scope=offering, project=new_project)

        marketplace_support_utils.init_offerings_and_resources(category, customer)
        self.assertFalse(
            marketplace_models.Resource.objects.filter(
                scope=offering, project=offering.project
            ).exists()
        )

    def test_create_missing_support_offerings_with_offering_plan(self):
        offering = support_factories.OfferingFactory()
        category = marketplace_factories.CategoryFactory()
        customer = structure_factories.CustomerFactory()
        offering_plan = support_factories.OfferingPlanFactory(
            template=offering.template, unit_price=offering.unit_price
        )

        marketplace_support_utils.init_offerings_and_resources(category, customer)
        self.assertTrue(
            marketplace_models.Plan.objects.filter(scope=offering_plan).exists()
        )
        self.assertEqual(marketplace_models.Plan.objects.count(), 1)
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=offering).exists()
        )

    def test_create_missing_support_offerings_with_changed_unit_price(self):
        offering = support_factories.OfferingFactory()
        category = marketplace_factories.CategoryFactory()
        customer = structure_factories.CustomerFactory()
        offering_plan = support_factories.OfferingPlanFactory(
            template=offering.template, unit_price=offering.unit_price
        )
        offering.unit_price += 10
        offering.save()

        marketplace_support_utils.init_offerings_and_resources(category, customer)
        self.assertTrue(
            marketplace_models.Plan.objects.filter(scope=offering_plan).exists()
        )
        self.assertEqual(marketplace_models.Plan.objects.count(), 2)
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=offering).exists()
        )
        resource = marketplace_models.Resource.objects.get(scope=offering)
        self.assertEqual(resource.plan.unit_price, offering.unit_price)

    def test_create_missing_support_offering_templates(self):
        offering_template = support_factories.OfferingTemplateFactory()
        category = marketplace_factories.CategoryFactory()
        customer = structure_factories.CustomerFactory()

        marketplace_support_utils.init_offerings_and_resources(category, customer)
        self.assertTrue(
            marketplace_models.Offering.objects.filter(scope=offering_template).exists()
        )
        self.assertTrue(
            marketplace_models.Offering.objects.filter(scope=offering_template).count(),
            1,
        )
        marketplace_support_utils.init_offerings_and_resources(category, customer)
        self.assertTrue(
            marketplace_models.Offering.objects.filter(scope=offering_template).count(),
            1,
        )
