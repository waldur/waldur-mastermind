from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories


class CounterQuotaFieldTest(TestCase):
    def test_target_model_instance_creation_increases_scope_counter_quota(self):
        customer = structure_factories.CustomerFactory()
        structure_factories.ProjectFactory(customer=customer)

        self.assertEqual(customer.get_quota_usage('nc_project_count'), 1)

    def test_target_model_instance_deletion_decreases_scope_counter_quota(self):
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)
        project.delete()

        self.assertEqual(customer.get_quota_usage('nc_project_count'), 0)
