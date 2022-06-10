import datetime
from unittest import mock

from django.utils import timezone
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm.models import Allocation
from waldur_slurm.parser import SlurmReportLine
from waldur_slurm.tests import factories as slurm_factories


class ComponentUsageTest(test.APITransactionTestCase):
    def setUp(self):
        fixture = structure_fixtures.ProjectFixture()
        service_settings = structure_factories.ServiceSettingsFactory(type='SLURM')
        offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, scope=service_settings
        )
        plan = marketplace_factories.PlanFactory(offering=offering)
        self.allocation = slurm_factories.AllocationFactory()
        self.resource = marketplace_models.Resource.objects.create(
            scope=self.allocation,
            offering=offering,
            plan=plan,
            project=fixture.project,
        )
        self.plan_period = marketplace_models.ResourcePlanPeriod.objects.create(
            resource=self.resource,
            plan=plan,
            start=timezone.make_aware(datetime.datetime.now()),
        )
        for component in manager.get_components(PLUGIN_NAME):
            offering_component = marketplace_models.OfferingComponent.objects.create(
                offering=offering,
                type=component.type,
                name=component.name,
                measured_unit=component.measured_unit,
                billing_type=marketplace_models.OfferingComponent.BillingTypes.USAGE,
            )
            marketplace_models.PlanComponent.objects.create(
                component=offering_component, plan=plan, price=3
            )

    def test_backend_triggers_usage_sync(self):
        self.allocation.backend_id = 'allocation1'
        self.allocation.save()

        backend = self.allocation.get_backend()
        backend.client = mock.Mock()
        backend.client.get_usage_report.return_value = [
            SlurmReportLine(
                'allocation1|cpu=1,node=1,gres/gpu=1,gres/gpu:tesla=1|00:01:00|user1|'
            ),
            SlurmReportLine(
                'allocation1|cpu=2,node=2,gres/gpu=2,gres/gpu:tesla=1|00:02:00|user2|'
            ),
        ]
        backend.sync_usage()
        self.allocation.refresh_from_db()

        self.assertEqual(self.allocation.cpu_usage, 1 + 2 * 2)
        self.assertEqual(self.allocation.gpu_usage, 1 + 2 * 2)

        self.assertTrue(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource, component__type='cpu'
            ).exists()
        )
        self.assertTrue(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource, component__type='gpu'
            ).exists()
        )

    def test_create_component_quota(self):
        self.allocation.cpu_usage = 1
        self.allocation.gpu_usage = 10
        self.allocation.ram_usage = 100
        self.allocation.save()

        for component in manager.get_components(PLUGIN_NAME):
            self.assertTrue(
                marketplace_models.ComponentQuota.objects.filter(
                    resource=self.resource, component__type=component.type
                ).exists()
            )
            quota = marketplace_models.ComponentQuota.objects.get(
                resource=self.resource, component__type=component.type
            )
            self.assertEqual(
                quota.limit, getattr(self.allocation, component.type + '_limit')
            )
            self.assertEqual(
                quota.usage, getattr(self.allocation, component.type + '_usage')
            )

    def test_invoice_item_is_not_created_when_allocation_creation_succeded(self):
        self.allocation.state = Allocation.States.CREATING
        self.allocation.save()

        self.allocation.set_ok()
        self.allocation.save()

        items = InvoiceItem.objects.filter(resource=self.resource)
        self.assertEqual(0, items.count())

    def test_invoice_item_is_created_for_each_component_when_usage_is_reported(self):
        self.allocation.cpu_usage = 1
        self.allocation.gpu_usage = 10
        self.allocation.ram_usage = 100
        self.allocation.save()

        for component in manager.get_components(PLUGIN_NAME):
            self.assertTrue(
                InvoiceItem.objects.filter(
                    resource=self.resource,
                    details__offering_component_type=component.type,
                ).exists()
            )

    def test_cpu_usage_is_converted_in_invoice_item_from_minutes_to_hours(self):
        self.allocation.cpu_usage = 60
        self.allocation.save()

        item = InvoiceItem.objects.get(
            resource=self.resource, details__offering_component_type='cpu'
        )
        self.assertEqual(item.quantity, 1)

    def test_gpu_usage_is_converted_in_invoice_item_from_minutes_to_hours(self):
        self.allocation.gpu_usage = 60
        self.allocation.save()

        item = InvoiceItem.objects.get(
            resource=self.resource, details__offering_component_type='gpu'
        )
        self.assertEqual(item.quantity, 1)

    def test_ram_usage_is_converted_in_invoice_item_from_minutes_to_hours_and_from_mb_to_gb(
        self,
    ):
        self.allocation.ram_usage = 60 * 1024
        self.allocation.save()

        item = InvoiceItem.objects.get(
            resource=self.resource, details__offering_component_type='ram'
        )
        self.assertEqual(item.quantity, 1)
