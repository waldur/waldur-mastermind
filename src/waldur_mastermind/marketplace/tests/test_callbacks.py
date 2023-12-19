from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.tests import factories


@freeze_time('2018-11-01')
class CallbacksTest(test.APITransactionTestCase):
    def test_when_resource_is_created_new_period_is_opened(self):
        # Arrange
        start = parse_datetime('2018-11-01')
        order = factories.OrderFactory(
            state=models.Order.States.EXECUTING,
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
        self.assertEqual(order.state, models.Order.States.DONE)

    def test_when_plan_is_changed_old_period_is_closed_new_is_opened(self):
        # Arrange
        old_start = parse_datetime('2018-10-01')
        new_start = parse_datetime('2018-11-01')

        old_plan = factories.PlanFactory()
        new_plan = factories.PlanFactory()

        resource = factories.ResourceFactory(plan=old_plan)
        old_period = models.ResourcePlanPeriod.objects.create(
            resource=resource, plan=old_plan, start=old_start, end=None
        )
        order = factories.OrderFactory(
            state=models.Order.States.EXECUTING,
            type=models.Order.Types.UPDATE,
            resource=resource,
            plan=new_plan,
        )

        # Act
        callbacks.resource_update_succeeded(resource)

        # Assert
        order.refresh_from_db()
        self.assertEqual(order.state, models.Order.States.DONE)

        old_period.refresh_from_db()
        self.assertEqual(old_period.end, new_start)

        self.assertTrue(
            models.ResourcePlanPeriod.objects.filter(
                resource=resource, plan=new_plan, start=new_start, end=None
            ).exists()
        )

    def test_when_resource_is_terminated_old_period_is_closed(self):
        # Arrange
        start = parse_datetime('2018-10-01')
        end = parse_datetime('2018-11-01')

        plan = factories.PlanFactory()
        resource = factories.ResourceFactory(plan=plan)

        period = models.ResourcePlanPeriod.objects.create(
            resource=resource, plan=plan, start=start, end=None
        )
        order = factories.OrderFactory(
            state=models.Order.States.EXECUTING,
            type=models.Order.Types.TERMINATE,
            resource=resource,
            plan=plan,
        )

        # Act
        callbacks.resource_deletion_succeeded(resource)

        # Assert
        order.refresh_from_db()
        self.assertEqual(order.state, models.Order.States.DONE)

        period.refresh_from_db()
        self.assertEqual(period.end, end)

    def test_when_resource_is_terminated_directly_old_period_is_closed(self):
        # Arrange
        start = parse_datetime('2018-10-01')
        end = parse_datetime('2018-11-01')

        plan = factories.PlanFactory()
        resource = factories.ResourceFactory(
            plan=plan, state=models.Resource.States.ERRED
        )

        period = models.ResourcePlanPeriod.objects.create(
            resource=resource, plan=plan, start=start, end=None
        )

        # Act
        resource.state = models.Resource.States.TERMINATED
        resource.save()

        # Assert
        period.refresh_from_db()
        self.assertEqual(period.end, end)
