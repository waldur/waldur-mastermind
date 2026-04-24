from unittest.mock import patch

from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes, ResourceStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.policy import models as policy_models
from waldur_openstack.tests.factories import InstanceFactory


@freeze_time("2018-11-01")
class CallbacksTest(test.APITestCase):
    def test_when_resource_is_created_new_period_is_opened(self):
        # Arrange
        start = parse_datetime("2018-11-01")
        order = factories.OrderFactory(
            state=OrderStates.EXECUTING,
        )

        # Act
        callbacks.resource_creation_succeeded(order.resource)

        # Assert
        self.assertTrue(
            models.ResourcePlanPeriod.objects.filter(
                resource=order.resource, plan=order.resource.plan, start=start, end=None
            ).exists()
        )

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)

    def test_when_plan_is_changed_old_period_is_closed_new_is_opened(self):
        # Arrange
        old_start = parse_datetime("2018-10-01")
        new_start = parse_datetime("2018-11-01")

        old_plan = factories.PlanFactory()
        new_plan = factories.PlanFactory()

        resource = factories.ResourceFactory(plan=old_plan)
        old_period = models.ResourcePlanPeriod.objects.create(
            resource=resource, plan=old_plan, start=old_start, end=None
        )
        order = factories.OrderFactory(
            state=OrderStates.EXECUTING,
            type=OrderTypes.UPDATE,
            resource=resource,
            plan=new_plan,
        )

        # Act
        callbacks.resource_update_succeeded(resource)

        # Assert
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)

        old_period.refresh_from_db()
        self.assertEqual(old_period.end, new_start)

        self.assertTrue(
            models.ResourcePlanPeriod.objects.filter(
                resource=resource, plan=new_plan, start=new_start, end=None
            ).exists()
        )

    def test_when_resource_is_terminated_old_period_is_closed(self):
        # Arrange
        start = parse_datetime("2018-10-01")
        end = parse_datetime("2018-11-01")

        plan = factories.PlanFactory()
        resource = factories.ResourceFactory(plan=plan)

        period = models.ResourcePlanPeriod.objects.create(
            resource=resource, plan=plan, start=start, end=None
        )
        order = factories.OrderFactory(
            state=OrderStates.EXECUTING,
            type=OrderTypes.TERMINATE,
            resource=resource,
            plan=plan,
        )

        # Act
        callbacks.resource_deletion_succeeded(resource)

        # Assert
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)

        period.refresh_from_db()
        self.assertEqual(period.end, end)

    def test_when_resource_is_terminated_directly_old_period_is_closed(self):
        # Arrange
        start = parse_datetime("2018-10-01")
        end = parse_datetime("2018-11-01")

        plan = factories.PlanFactory()
        resource = factories.ResourceFactory(plan=plan, state=ResourceStates.ERRED)

        period = models.ResourcePlanPeriod.objects.create(
            resource=resource, plan=plan, start=start, end=None
        )

        # Act
        resource.state = ResourceStates.TERMINATED
        resource.save()

        # Assert
        period.refresh_from_db()
        self.assertEqual(period.end, end)

    def test_error_message_is_propagated(self):
        # Arrange
        error_message = "Provision failed."
        error_traceback = "Invalid credentials."

        resource = factories.ResourceFactory(
            scope=InstanceFactory(
                error_message="Provision failed.",
                error_traceback="Invalid credentials.",
            )
        )

        order = factories.OrderFactory(
            state=OrderStates.EXECUTING,
            type=OrderTypes.CREATE,
            resource=resource,
        )

        # Act
        callbacks.resource_creation_failed(resource)

        # Assert
        order.refresh_from_db()
        self.assertEqual(order.error_message, error_message)
        self.assertEqual(order.error_traceback, error_traceback)


@freeze_time("2018-11-01")
class RestoreCallbacksTest(test.APITestCase):
    def test_restore_succeeded_transitions_order_to_done(self):
        resource = factories.ResourceFactory(state=ResourceStates.CREATING)
        order = factories.OrderFactory(
            state=OrderStates.EXECUTING,
            type=OrderTypes.RESTORE,
            resource=resource,
        )

        callbacks.resource_restore_succeeded(resource)

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)

    def test_restore_succeeded_sets_resource_ok(self):
        resource = factories.ResourceFactory(state=ResourceStates.CREATING)
        factories.OrderFactory(
            state=OrderStates.EXECUTING,
            type=OrderTypes.RESTORE,
            resource=resource,
        )

        callbacks.resource_restore_succeeded(resource)

        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.OK)

    def test_restore_failed_sets_resource_erred(self):
        resource = factories.ResourceFactory(state=ResourceStates.CREATING)
        order = factories.OrderFactory(
            state=OrderStates.EXECUTING,
            type=OrderTypes.RESTORE,
            resource=resource,
        )

        callbacks.resource_restore_failed(resource)

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.ERRED)

        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.ERRED)

    def test_restore_canceled_sets_resource_terminated(self):
        resource = factories.ResourceFactory(state=ResourceStates.CREATING)
        order = factories.OrderFactory(
            state=OrderStates.EXECUTING,
            type=OrderTypes.RESTORE,
            resource=resource,
        )

        callbacks.resource_restore_canceled(resource)

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.CANCELED)

        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.TERMINATED)

    def test_sync_order_state_dispatches_restore_done(self):
        resource = factories.ResourceFactory(state=ResourceStates.CREATING)
        order = factories.OrderFactory(
            state=OrderStates.EXECUTING,
            type=OrderTypes.RESTORE,
            resource=resource,
        )

        callbacks.sync_order_state(order, OrderStates.DONE)

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)

        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.OK)


class LimitUpdateTriggersPolicyReevaluationTest(test.APITestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory(
            type="Marketplace.Slurm",
            plugin_options={"supports_downscaling": True, "supports_pausing": True},
        )
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="node-hours",
            name="Node hours",
            billing_type="limit",
        )
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.UPDATING,
            limits={"node-hours": 1000},
            downscaled=True,
        )
        self.policy = policy_models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,
            period=3,
        )
        policy_models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )

    def _create_update_order(self, **kwargs):
        defaults = dict(
            state=OrderStates.EXECUTING,
            type=OrderTypes.UPDATE,
            resource=self.resource,
            offering=self.offering,
            project=self.resource.project,
        )
        defaults.update(kwargs)
        return factories.OrderFactory(**defaults)

    @patch("waldur_mastermind.policy.tasks.evaluate_resource_against_policy.delay")
    def test_limit_increase_on_downscaled_resource_triggers_reevaluation(
        self, mock_delay
    ):
        order = self._create_update_order(limits={"node-hours": 2000})

        with self.captureOnCommitCallbacks(execute=True):
            callbacks.resource_update_succeeded(order.resource)

        mock_delay.assert_called_once_with(
            str(self.resource.uuid), str(self.policy.uuid)
        )

    @patch("waldur_mastermind.policy.tasks.evaluate_resource_against_policy.delay")
    def test_limit_increase_on_paused_resource_triggers_reevaluation(self, mock_delay):
        self.resource.downscaled = False
        self.resource.paused = True
        self.resource.save()

        order = self._create_update_order(limits={"node-hours": 2000})

        with self.captureOnCommitCallbacks(execute=True):
            callbacks.resource_update_succeeded(order.resource)

        mock_delay.assert_called_once_with(
            str(self.resource.uuid), str(self.policy.uuid)
        )

    @patch("waldur_mastermind.policy.tasks.evaluate_resource_against_policy.delay")
    def test_limit_increase_on_normal_resource_does_not_trigger_reevaluation(
        self, mock_delay
    ):
        self.resource.downscaled = False
        self.resource.paused = False
        self.resource.save()

        order = self._create_update_order(limits={"node-hours": 2000})

        with self.captureOnCommitCallbacks(execute=True):
            callbacks.resource_update_succeeded(order.resource)

        mock_delay.assert_not_called()

    @patch("waldur_mastermind.policy.tasks.evaluate_resource_against_policy.delay")
    def test_plan_change_without_limit_change_does_not_trigger_reevaluation(
        self, mock_delay
    ):
        new_plan = factories.PlanFactory()
        order = self._create_update_order(plan=new_plan)

        with self.captureOnCommitCallbacks(execute=True):
            callbacks.resource_update_succeeded(order.resource)

        mock_delay.assert_not_called()
