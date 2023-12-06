from django.core.exceptions import ObjectDoesNotExist
from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm import models as slurm_models
from waldur_slurm.tests import fixtures as slurm_fixtures


class AllocationCreateTest(test.APITransactionTestCase):
    def setUp(self):
        fixture = slurm_fixtures.SlurmFixture()
        offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, scope=fixture.settings
        )
        plan = marketplace_factories.PlanFactory(offering=offering)
        order = marketplace_factories.OrderFactory(
            project=fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
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
                plan=plan,
                component=component,
            )

        # Create SPL
        self.fixture = fixture
        self.order = order
        self.offering = offering

    def test_create_allocation_if_order_is_approved(self):
        self.trigger_creation()
        self.assertTrue(
            slurm_models.Allocation.objects.filter(
                name=self.order.attributes['name']
            ).exists()
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.state, marketplace_models.Order.States.EXECUTING)

    def test_not_create_allocation_if_scope_is_invalid(self):
        self.offering.scope = None
        self.offering.save()
        self.trigger_creation()

        self.assertFalse(
            slurm_models.Allocation.objects.filter(
                name=self.order.attributes['name']
            ).exists()
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.state, marketplace_models.Order.States.ERRED)

    def test_allocation_state_is_synchronized(self):
        self.trigger_creation()

        self.order.refresh_from_db()
        instance = self.order.resource.scope

        instance.begin_creating()
        instance.save()

        instance.set_ok()
        instance.save()

        self.order.refresh_from_db()
        self.assertEqual(self.order.state, marketplace_models.Order.States.DONE)

        self.order.resource.refresh_from_db()
        self.assertEqual(
            self.order.resource.state, marketplace_models.Resource.States.OK
        )

    def trigger_creation(self):
        marketplace_utils.process_order(self.order, self.fixture.staff)


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
            resource=self.resource,
            type=marketplace_models.RequestTypeMixin.Types.TERMINATE,
        )

    def test_deletion_is_scheduled(self):
        self.trigger_deletion()
        self.assertEqual(self.order.state, marketplace_models.Order.States.EXECUTING)
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATING
        )
        self.assertEqual(
            self.allocation.state, slurm_models.Allocation.States.DELETION_SCHEDULED
        )

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.allocation.delete()

        self.order.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(self.order.state, marketplace_models.Order.States.DONE)
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATED
        )
        self.assertRaises(ObjectDoesNotExist, self.allocation.refresh_from_db)

    def trigger_deletion(self):
        marketplace_utils.process_order(self.order, self.fixture.staff)

        self.order.refresh_from_db()
        self.resource.refresh_from_db()
        self.allocation.refresh_from_db()
