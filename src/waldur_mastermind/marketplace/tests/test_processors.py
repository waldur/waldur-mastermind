from unittest import mock

from dateutil.relativedelta import relativedelta
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import utils
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


class ProcessorsTest(test.APITransactionTestCase):
    def test_creating_of_resource(self):
        user = structure_factories.UserFactory(is_staff=True)
        success = []
        failed = []

        for offering_type in manager.get_offering_types():
            offering = factories.OfferingFactory(type=offering_type)
            order = factories.OrderFactory(
                offering=offering, state=OrderStates.EXECUTING
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

        order = factories.OrderFactory(offering=offering, state=OrderStates.EXECUTING)
        resource = order.resource

        utils.process_order(order, user)

        order.refresh_from_db()
        resource.refresh_from_db()

        self.assertEqual(OrderStates.ERRED, order.state)
        self.assertEqual(ResourceStates.ERRED, resource.state)

    @mock.patch(
        "waldur_mastermind.marketplace.processors.BasicCreateResourceProcessor.process_order"
    )
    def test_resource_marked_as_erred_when_order_failed(self, process_order_mock):
        process_order_mock.side_effect = Exception("Error!")

        user = structure_factories.UserFactory(is_staff=True)
        offering = factories.OfferingFactory(type=BASIC_OFFERING)

        order = factories.OrderFactory(offering=offering, state=OrderStates.EXECUTING)
        resource = order.resource

        utils.process_order(order, user)

        order.refresh_from_db()
        resource.refresh_from_db()

        self.assertEqual(OrderStates.ERRED, order.state)
        self.assertEqual(ResourceStates.ERRED, resource.state)

    def test_set_resource_options(self):
        # NOTE: This test verifies that resource options logic has been moved from processor
        # to OrderCreateSerializer. Since we're using factories (not the API), we manually
        # set options to simulate what the serializer would do.
        user = structure_factories.UserFactory()

        offering_type = manager.get_offering_types()[-1]
        offering = factories.OfferingFactory(type=offering_type)
        offering.resource_options = {
            "options": {"cpu": None, "ram": None},
            "order": [],
        }
        offering.save()

        order = factories.OrderFactory(
            offering=offering,
            state=OrderStates.EXECUTING,
            attributes={"cpu": 1, "storage": 10},
        )

        # Manually set resource options as OrderCreateSerializer would do
        # (since factories don't go through the serializer)
        resource = order.resource
        resource.options = {}
        for resource_option in offering.resource_options.get("options", {}).keys():
            if resource_option in order.attributes:
                resource.options[resource_option] = order.attributes[resource_option]
        resource.save()

        # Verify options are set correctly before processing
        self.assertTrue(isinstance(order.resource.options, dict))
        self.assertFalse("storage" in order.resource.options.keys())
        self.assertTrue("cpu" in order.resource.options.keys())
        self.assertEqual(order.resource.options["cpu"], 1)

        # Processing the order should not change the options (confirming processor no longer handles this)
        utils.process_order(order, user)
        order.refresh_from_db()

        self.assertTrue(isinstance(order.resource.options, dict))
        self.assertFalse("storage" in order.resource.options.keys())
        self.assertTrue("cpu" in order.resource.options.keys())
        self.assertEqual(order.resource.options["cpu"], 1)


class UpdateResourceProcessorTest(test.APITransactionTestCase):
    def setUp(self):
        # Use a fixture that provides all necessary objects
        self.fixture = MarketplaceFixture()
        self.user = self.fixture.staff

        # Use the BASIC_OFFERING type, as its processor inherits from
        # AbstractUpdateResourceProcessor and has a simple, synchronous
        # update_limits_process method that returns True.
        self.offering = self.fixture.offering
        self.offering.type = BASIC_OFFERING
        self.offering.save()

        self.plan = self.fixture.plan
        self.plan.offering = self.offering
        self.plan.save()

    @freeze_time("2024-06-01")
    def test_renewal_order_updates_limits_end_date_and_history(self):
        # Arrange
        initial_end_date = timezone.now().date() + relativedelta(months=1)
        resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            limits={"storage": 100},
            end_date=initial_end_date,
        )

        new_end_date = initial_end_date + relativedelta(months=12)
        order_attributes = {
            "action": "renew",
            "old_limits": resource.limits,
            "new_end_date": new_end_date.isoformat(),
            "old_end_date": initial_end_date.isoformat(),
            "renewal_cost": 24000.0,
        }

        order = factories.OrderFactory(
            resource=resource,
            offering=self.offering,
            plan=self.plan,
            state=OrderStates.EXECUTING,
            type=OrderTypes.UPDATE,
            limits={"storage": 200},
            attributes=order_attributes,
        )

        # Act
        utils.process_order(order, self.user)

        # Assert
        resource.refresh_from_db()

        # 1. Verify limits are updated
        self.assertEqual(resource.limits, {"storage": 200})

        # 2. Verify end_date is updated
        self.assertEqual(resource.end_date, new_end_date)
        self.assertEqual(resource.end_date_requested_by, self.user)

        # 3. Verify renewal history is created
        self.assertIn("renewal_history", resource.attributes)
        history = resource.attributes["renewal_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["type"], "renewal")
        self.assertEqual(history[0]["new_end_date"], new_end_date.isoformat())
        self.assertEqual(history[0]["new_limits"], {"storage": 200})

    @freeze_time("2024-06-01")
    def test_limit_update_order_only_updates_limits(self):
        # Arrange
        initial_end_date = timezone.now().date() + relativedelta(months=1)
        resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            limits={"storage": 100},
            end_date=initial_end_date,
        )

        order_attributes = {
            "old_limits": resource.limits,
            # CRITICAL: 'action' is not 'renew'
        }

        order = factories.OrderFactory(
            resource=resource,
            offering=self.offering,
            plan=self.plan,
            state=OrderStates.EXECUTING,
            type=OrderTypes.UPDATE,
            limits={"storage": 200},
            attributes=order_attributes,
        )

        # Act
        utils.process_order(order, self.user)

        # Assert
        resource.refresh_from_db()

        # 1. Verify limits are updated
        self.assertEqual(resource.limits, {"storage": 200})

        # 2. Verify end_date is NOT changed
        self.assertEqual(resource.end_date, initial_end_date)

        # 3. Verify renewal history is NOT created
        self.assertNotIn("renewal_history", resource.attributes)
