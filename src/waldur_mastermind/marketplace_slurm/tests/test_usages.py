from django.test.utils import override_settings
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures, factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm.tests import factories as slurm_factories


class BaseTest(test.APITransactionTestCase):
    def setUp(self):
        fixture = structure_fixtures.ProjectFixture()
        service_settings = structure_factories.ServiceSettingsFactory(type='SLURM')
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME, scope=service_settings)
        plan = marketplace_factories.PlanFactory(offering=offering)
        order = marketplace_factories.OrderFactory(project=fixture.project)
        self.allocation = slurm_factories.AllocationFactory()
        self.order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=offering,
            scope=self.allocation,
            plan=plan,
        )
        for component in manager.get_components(PLUGIN_NAME):
            marketplace_models.OfferingComponent.objects.create(
                offering=offering,
                type=component.type,
                name=component.name,
                measured_unit=component.measured_unit,
                billing_type=marketplace_models.OfferingComponent.BillingTypes.USAGE,
            )


class ComponentUsageTest(BaseTest):
    @override_settings(ALLOWED_HOSTS=['localhost'])
    def test_create_component_usage(self):
        slurm_factories.AllocationUsageFactory(allocation=self.allocation)
        self.assertTrue(marketplace_models.ComponentUsage.objects
                        .filter(order_item=self.order_item, component__type='cpu').exists())
        self.assertTrue(marketplace_models.ComponentUsage.objects
                        .filter(order_item=self.order_item, component__type='gpu').exists())
        self.assertTrue(marketplace_models.ComponentUsage.objects
                        .filter(order_item=self.order_item, component__type='ram').exists())

    @override_settings(ALLOWED_HOSTS=['localhost'])
    def test_not_create_component_usage_if_creat_other_allocation_usage(self):
        slurm_factories.AllocationUsageFactory()
        self.assertFalse(marketplace_models.ComponentUsage.objects
                         .filter(order_item=self.order_item, component__type='cpu').exists())
        self.assertFalse(marketplace_models.ComponentUsage.objects
                         .filter(order_item=self.order_item, component__type='gpu').exists())
        self.assertFalse(marketplace_models.ComponentUsage.objects
                         .filter(order_item=self.order_item, component__type='ram').exists())


class ComponentQuotaTest(BaseTest):
    @override_settings(ALLOWED_HOSTS=['localhost'])
    def test_create_component_quota(self):
        self.allocation.cpu_usage = 1
        self.allocation.gpu_usage = 10
        self.allocation.ram_usage = 100
        self.allocation.save()

        for component in manager.get_components(PLUGIN_NAME):
            self.assertTrue(marketplace_models.ComponentQuota.objects
                            .filter(order_item=self.order_item, component__type=component.type).exists())
            quota = marketplace_models.ComponentQuota.objects\
                .get(order_item=self.order_item, component__type=component.type)
            self.assertEqual(quota.limit, getattr(self.allocation, component.type + '_limit'))
            self.assertEqual(quota.usage, getattr(self.allocation, component.type + '_usage'))
