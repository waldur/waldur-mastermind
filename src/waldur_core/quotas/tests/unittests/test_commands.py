from django.core.management import call_command
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories


class RecalculateCommandTest(TestCase):
    def test_counter_quota_recalculation(self):
        customer = structure_factories.CustomerFactory()
        structure_factories.ProjectFactory(customer=customer)

        customer.set_quota_usage('nc_project_count', 10)

        call_command('recalculatequotas')
        self.assertEqual(customer.get_quota_usage('nc_project_count'), 1)

    def test_aggregator_quota_recalculation(self):
        customer = structure_factories.CustomerFactory()
        structure_factories.ProjectFactory(customer=customer)

        customer.set_quota_usage('nc_resource_count', 10)

        call_command('recalculatequotas')
        self.assertEqual(customer.get_quota_usage('nc_resource_count'), 0)
