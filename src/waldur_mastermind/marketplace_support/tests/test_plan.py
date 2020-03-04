from rest_framework import test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support import models as support_models


class OfferingPlanTest(test.APITransactionTestCase):
    def test_offering_plan_is_created(self):
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        plan = marketplace_factories.PlanFactory(offering=offering)
        plan.refresh_from_db()

        self.assertTrue(isinstance(plan.scope, support_models.OfferingPlan))
        self.assertEqual(plan.unit_price, plan.scope.unit_price)
        self.assertTrue(plan.unit, plan.scope.unit)
        self.assertTrue(plan.name, plan.scope.name)
