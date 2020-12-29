import copy

from django.conf import settings
from django.test import override_settings
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_mastermind.marketplace_slurm.management.commands.import_allocations import (
    import_allocation,
)
from waldur_mastermind.marketplace_slurm.management.commands.import_slurm_service_settings import (
    import_slurm_service_settings,
)
from waldur_mastermind.slurm_invoices.tests import factories as slurm_invoices_factories
from waldur_slurm.tests import factories as slurm_factories


def override_plugin_settings(**kwargs):
    plugin_settings = copy.deepcopy(settings.WALDUR_MARKETPLACE_SLURM)
    plugin_settings.update(kwargs)
    return override_settings(WALDUR_MARKETPLACE_SLURM=plugin_settings)


class AllocationImportTest(test.APITransactionTestCase):
    def setUp(self):
        super(AllocationImportTest, self).setUp()
        self.category = marketplace_factories.CategoryFactory(title='SLURM')
        self.decorator = override_plugin_settings(CATEGORY_UUID=self.category.uuid.hex,)
        self.decorator.enable()

    def tearDown(self):
        super(AllocationImportTest, self).tearDown()
        self.decorator.disable()

    def test_allocation_import(self):
        allocation = slurm_factories.AllocationFactory()
        package = slurm_invoices_factories.SlurmPackageFactory(
            service_settings=allocation.service_settings,
            cpu_price=5,
            gpu_price=15,
            ram_price=30,
        )
        allocation_usage = slurm_factories.AllocationUsageFactory(
            allocation=allocation,
            year=allocation.created.year,
            month=allocation.created.month,
            cpu_usage=1,
            gpu_usage=5,
            ram_usage=10,
        )
        customer = structure_factories.CustomerFactory()

        import_slurm_service_settings(customer)
        import_allocation()

        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=allocation).exists()
        )
        self.assertEqual(marketplace_models.Resource.objects.count(), 1)
        resource = marketplace_models.Resource.objects.get(scope=allocation)
        self.assertEqual(
            resource.plan.components.get(component__type='cpu').price, package.cpu_price
        )
        self.assertEqual(
            resource.plan.components.get(component__type='gpu').price, package.gpu_price
        )
        self.assertEqual(
            resource.plan.components.get(component__type='ram').price, package.ram_price
        )
        self.assertEqual(marketplace_models.ComponentUsage.objects.count(), 3)
        self.assertEqual(
            marketplace_models.ComponentUsage.objects.get(component__type='cpu').usage,
            allocation_usage.cpu_usage,
        )
        self.assertEqual(
            marketplace_models.ComponentUsage.objects.get(component__type='gpu').usage,
            allocation_usage.gpu_usage,
        )
        self.assertEqual(
            marketplace_models.ComponentUsage.objects.get(component__type='ram').usage,
            allocation_usage.ram_usage,
        )

    def test_dry_run_allocation_import(self):
        allocation = slurm_factories.AllocationFactory()
        slurm_invoices_factories.SlurmPackageFactory(
            service_settings=allocation.service_settings,
            cpu_price=5,
            gpu_price=15,
            ram_price=30,
        )
        customer = structure_factories.CustomerFactory()

        import_slurm_service_settings(customer)
        allocation_counter = import_allocation(True)
        self.assertEqual(allocation_counter, 1)
        self.assertFalse(
            marketplace_models.Resource.objects.filter(scope=allocation).exists()
        )

    def test_service_settings_import(self):
        allocation = slurm_factories.AllocationFactory()
        customer = structure_factories.CustomerFactory()
        offerings_counter = import_slurm_service_settings(customer)
        self.assertEqual(offerings_counter, 1)
        offering = marketplace_models.Offering.objects.get(type=PLUGIN_NAME)
        self.assertEqual(offering.components.count(), 3)
        self.assertEqual(allocation.service_settings, offering.scope)
        self.assertTrue(offering.components.filter(type='cpu').exists())
        self.assertTrue(offering.components.filter(type='cpu').exists())
        self.assertTrue(offering.components.filter(type='cpu').exists())

    def test_dry_run_service_settings_import(self):
        slurm_factories.AllocationFactory()
        customer = structure_factories.CustomerFactory()
        offerings_counter = import_slurm_service_settings(customer, True)
        self.assertEqual(offerings_counter, 1)
        self.assertEqual(marketplace_models.Offering.objects.count(), 0)

    def test_resource_plan_imported_once(self):
        allocation = slurm_factories.AllocationFactory()
        allocation = slurm_factories.AllocationFactory(
            service_project_link=allocation.service_project_link
        )
        slurm_invoices_factories.SlurmPackageFactory(
            service_settings=allocation.service_settings,
            cpu_price=5,
            gpu_price=15,
            ram_price=30,
        )
        customer = structure_factories.CustomerFactory()

        import_slurm_service_settings(customer)
        import_allocation()

        self.assertEqual(marketplace_models.Plan.objects.count(), 1)
        self.assertEqual(marketplace_models.PlanComponent.objects.count(), 3)

    def test_inactive_allocation_import(self):
        allocation = slurm_factories.AllocationFactory(is_active=False)
        slurm_invoices_factories.SlurmPackageFactory(
            service_settings=allocation.service_settings,
            cpu_price=5,
            gpu_price=15,
            ram_price=30,
        )
        customer = structure_factories.CustomerFactory()

        import_slurm_service_settings(customer)
        import_allocation()

        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=allocation).exists()
        )
        self.assertEqual(marketplace_models.Resource.objects.count(), 1)
        resource = marketplace_models.Resource.objects.get(scope=allocation)
        self.assertEqual(resource.state, marketplace_models.Resource.States.TERMINATED)

    def test_import_limits(self):
        allocation = slurm_factories.AllocationFactory()
        slurm_invoices_factories.SlurmPackageFactory(
            service_settings=allocation.service_settings,
            cpu_price=5,
            gpu_price=15,
            ram_price=30,
        )
        customer = structure_factories.CustomerFactory()

        import_slurm_service_settings(customer)
        import_allocation()

        resource = marketplace_models.Resource.objects.get(scope=allocation)
        self.assertEqual(resource.quotas.count(), 3)
        self.assertEqual(
            resource.quotas.filter(component__type='cpu').get().limit,
            allocation.cpu_limit,
        )
        self.assertEqual(
            resource.quotas.filter(component__type='gpu').get().limit,
            allocation.gpu_limit,
        )
        self.assertEqual(
            resource.quotas.filter(component__type='ram').get().limit,
            allocation.ram_limit,
        )
