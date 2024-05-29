from unittest import mock

from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import PLUGIN_NAME, models, utils
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories


class ProcessorsTest(test.APITransactionTestCase):
    def test_creating_of_resource(self):
        user = structure_factories.UserFactory(is_staff=True)
        success = []
        failed = []

        for offering_type in manager.get_offering_types():
            offering = factories.OfferingFactory(type=offering_type)
            order = factories.OrderFactory(
                offering=offering, state=models.Order.States.EXECUTING
            )
            utils.process_order(order, user)
            order.refresh_from_db()

            if not order.resource:
                failed.append(offering_type)
            else:
                success.append(offering_type)

        self.assertFalse(failed, f"failed: {failed}, success {success}")

    def test_resource_marked_as_erred_when_order_processor_is_not_found(self):
        user = structure_factories.UserFactory(is_staff=True)
        offering = factories.OfferingFactory(type="ABC")

        order = factories.OrderFactory(
            offering=offering, state=models.Order.States.EXECUTING
        )
        resource = order.resource

        utils.process_order(order, user)

        order.refresh_from_db()
        resource.refresh_from_db()

        self.assertEqual(models.Order.States.ERRED, order.state)
        self.assertEqual(models.Resource.States.ERRED, resource.state)

    @mock.patch(
        "waldur_mastermind.marketplace.processors.BasicCreateResourceProcessor.process_order"
    )
    def test_resource_marked_as_erred_when_order_failed(self, process_order_mock):
        process_order_mock.side_effect = Exception("Error!")

        user = structure_factories.UserFactory(is_staff=True)
        offering = factories.OfferingFactory(type=PLUGIN_NAME)

        order = factories.OrderFactory(
            offering=offering, state=models.Order.States.EXECUTING
        )
        resource = order.resource

        utils.process_order(order, user)

        order.refresh_from_db()
        resource.refresh_from_db()

        self.assertEqual(models.Order.States.ERRED, order.state)
        self.assertEqual(models.Resource.States.ERRED, resource.state)

    def test_set_resource_options(self):
        user = structure_factories.UserFactory()

        for offering_type in manager.get_offering_types():
            offering = factories.OfferingFactory(type=offering_type)
            offering.resource_options = {
                "options": {"cpu": None, "ram": None},
                "order": [],
            }
            order = factories.OrderFactory(
                offering=offering,
                state=models.Order.States.EXECUTING,
                attributes={"cpu": 1, "storage": 10},
            )
            utils.process_order(order, user)
            order.refresh_from_db()

        self.assertTrue(isinstance(order.resource.options, dict))
        self.assertFalse("storage" in order.resource.options.keys())
        self.assertTrue("cpu" in order.resource.options.keys())
