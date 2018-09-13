from django.test.utils import override_settings
from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import tasks as marketplace_tasks, models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm import models as slurm_models
from waldur_slurm.tests import fixtures as slurm_fixtures


class SlurmOrderTest(test.APITransactionTestCase):
    @override_settings(ALLOWED_HOSTS=['localhost'])
    def test_create_allocation_if_order_item_is_approved(self):
        fixture = slurm_fixtures.SlurmFixture()
        service_settings = fixture.service.settings
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME, options={'order': []},
                                                         scope=service_settings)
        order = marketplace_factories.OrderFactory(project=fixture.project)
        order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=offering,
            attributes={'cpu_limit': 10,
                        'gpu_limit': 100,
                        'ram_limit': 1000, }
        )

        # Create SPL
        fixture.spl

        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.assertTrue(slurm_models.Allocation.objects.filter(name=offering.name).exists())

    @override_settings(ALLOWED_HOSTS=['localhost'])
    def test_not_create_allocation_if_scope_is_invalid(self):
        fixture = slurm_fixtures.SlurmFixture()
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME, options={'order': []})
        order = marketplace_factories.OrderFactory(project=fixture.project,
                                                   state=marketplace_models.Order.States.EXECUTING)
        order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=offering,
            attributes={}
        )

        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)
        self.assertFalse(slurm_models.Allocation.objects.filter(name=offering.name).exists())
        order_item.refresh_from_db()
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.ERRED)
