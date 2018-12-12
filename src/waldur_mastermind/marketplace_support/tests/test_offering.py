from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests import factories as support_factories
from waldur_mastermind.support.tests.base import BaseTest
from waldur_mastermind.marketplace_support import utils as marketplace_support_utils


class OfferingTemplateCreateTest(test.APITransactionTestCase):

    def test_offering_template_is_created_for_valid_type(self):
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        offering.refresh_from_db()
        template = support_models.OfferingTemplate.objects.get(name=offering.name)
        self.assertTrue(offering.scope, template)

    def test_offering_template_is_not_created_for_invalid_type(self):
        offering = marketplace_factories.OfferingFactory()
        offering.refresh_from_db()
        self.assertIsNone(offering.scope)


class SupportOfferingTest(BaseTest):
    def test_offering_set_state_done(self):
        offering = support_factories.OfferingFactory()
        resource = marketplace_factories.ResourceFactory(scope=offering)
        order_item = marketplace_factories.OrderItemFactory(resource=resource)
        order_item.set_state_executing()
        order_item.save()

        order_item.order.state = marketplace_models.Order.States.EXECUTING
        order_item.order.save()
        offering.issue.status = 'Completed'
        offering.issue.resolution = 'Done'
        offering.issue.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)
        offering.refresh_from_db()
        self.assertEqual(offering.state, offering.States.OK)


class SupportOfferingResourceTest(BaseTest):
    def test_create_missing_support_offerings(self):
        offering = support_factories.OfferingFactory()
        category = marketplace_factories.CategoryFactory()
        customer = structure_factories.CustomerFactory()

        marketplace_support_utils.init_offerings_and_resources(category, customer)
        self.assertTrue(marketplace_models.Resource.objects.filter(scope=offering, project=offering.project).exists())

    def test_filter_when_creating_missing_support_offerings(self):
        offering = support_factories.OfferingFactory()
        category = marketplace_factories.CategoryFactory()
        customer = structure_factories.CustomerFactory()
        new_project = structure_factories.ProjectFactory()
        marketplace_factories.ResourceFactory(scope=offering, project=new_project)

        marketplace_support_utils.init_offerings_and_resources(category, customer)
        self.assertFalse(marketplace_models.Resource.objects.filter(scope=offering, project=offering.project).exists())

    def test_create_missing_support_offerings_with_offering_plan(self):
        offering = support_factories.OfferingFactory()
        category = marketplace_factories.CategoryFactory()
        customer = structure_factories.CustomerFactory()
        offering_plan = support_factories.OfferingPlanFactory(template=offering.template,
                                                              unit_price=offering.unit_price)

        marketplace_support_utils.init_offerings_and_resources(category, customer)
        self.assertTrue(marketplace_models.Plan.objects.filter(scope=offering_plan).exists())
        self.assertEqual(marketplace_models.Plan.objects.count(), 1)
        self.assertTrue(marketplace_models.Resource.objects.filter(scope=offering).exists())

    def test_create_missing_support_offerings_with_changed_unit_price(self):
        offering = support_factories.OfferingFactory()
        category = marketplace_factories.CategoryFactory()
        customer = structure_factories.CustomerFactory()
        offering_plan = support_factories.OfferingPlanFactory(template=offering.template,
                                                              unit_price=offering.unit_price)
        offering.unit_price += 10
        offering.save()

        marketplace_support_utils.init_offerings_and_resources(category, customer)
        self.assertTrue(marketplace_models.Plan.objects.filter(scope=offering_plan).exists())
        self.assertEqual(marketplace_models.Plan.objects.count(), 2)
        self.assertTrue(marketplace_models.Resource.objects.filter(scope=offering).exists())
        resource = marketplace_models.Resource.objects.get(scope=offering)
        self.assertEqual(resource.plan.unit_price, offering.unit_price)
