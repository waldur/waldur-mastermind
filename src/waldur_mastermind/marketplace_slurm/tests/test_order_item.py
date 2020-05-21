from django.core.exceptions import ObjectDoesNotExist
from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm import models as slurm_models
from waldur_slurm.tests import fixtures as slurm_fixtures


class AllocationCreateTest(test.APITransactionTestCase):
    def setUp(self):
        fixture = slurm_fixtures.SlurmFixture()
        service_settings = fixture.service.settings
        offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, scope=service_settings
        )
        plan = marketplace_factories.PlanFactory(offering=offering)
        order = marketplace_factories.OrderFactory(
            project=fixture.project, state=marketplace_models.Order.States.EXECUTING
        )
        order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=offering,
            limits={
                component.type: 10 for component in manager.get_components(PLUGIN_NAME)
            },
            attributes={'name': 'My-first-allocation'},
        )
        for component in manager.get_components(PLUGIN_NAME):
            component = marketplace_models.OfferingComponent.objects.create(
                offering=offering,
                type=component.type,
                name=component.name,
                measured_unit=component.measured_unit,
            )
            marketplace_models.PlanComponent.objects.create(
                plan=plan, component=component,
            )

        # Create SPL
        fixture.spl
        self.fixture = fixture
        self.order_item = order_item
        self.offering = offering

    def test_create_allocation_if_order_item_is_approved(self):
        self.trigger_creation()
        self.assertTrue(
            slurm_models.Allocation.objects.filter(
                name=self.order_item.attributes['name']
            ).exists()
        )

        self.order_item.refresh_from_db()
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )

    def test_not_create_allocation_if_scope_is_invalid(self):
        self.offering.scope = None
        self.offering.save()
        self.trigger_creation()

        self.assertFalse(
            slurm_models.Allocation.objects.filter(
                name=self.order_item.attributes['name']
            ).exists()
        )

        self.order_item.refresh_from_db()
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.ERRED
        )

    def test_allocation_state_is_synchronized(self):
        self.trigger_creation()

        self.order_item.refresh_from_db()
        instance = self.order_item.resource.scope

        instance.begin_creating()
        instance.save()

        instance.set_ok()
        instance.save()

        self.order_item.refresh_from_db()
        self.assertEqual(self.order_item.state, self.order_item.States.DONE)

        self.order_item.resource.refresh_from_db()
        self.assertEqual(
            self.order_item.resource.state, marketplace_models.Resource.States.OK
        )

        self.order_item.order.refresh_from_db()
        self.assertEqual(
            self.order_item.order.state, marketplace_models.Order.States.DONE
        )

    def trigger_creation(self):
        serialized_order = core_utils.serialize_instance(self.order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)


class AllocationDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = slurm_fixtures.SlurmFixture()
        self.allocation = self.fixture.allocation

        self.offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.allocation, offering=self.offering
        )
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )
        self.order_item = marketplace_factories.OrderItemFactory(
            resource=self.resource,
            type=marketplace_models.RequestTypeMixin.Types.TERMINATE,
        )

    def test_deletion_is_scheduled(self):
        self.trigger_deletion()
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATING
        )
        self.assertEqual(
            self.allocation.state, slurm_models.Allocation.States.DELETION_SCHEDULED
        )

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.allocation.delete()

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.DONE
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATED
        )
        self.assertRaises(ObjectDoesNotExist, self.allocation.refresh_from_db)

    def trigger_deletion(self):
        serialized_order = core_utils.serialize_instance(self.order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.allocation.refresh_from_db()
