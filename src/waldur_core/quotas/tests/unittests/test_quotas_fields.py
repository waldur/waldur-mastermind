# Tests from this module use structure and OpenStack models with their quotas
# to test quotas behaviour. Ideally we need to test quotas based on some abstract or
# test only models, but it is not really supported by Django.

from django.test import TestCase

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories


class QuotaFieldTest(TestCase):

    def setUp(self):
        self.customer_quotas_names = [f.name for f in structure_models.Customer.get_quotas_fields()]

    def test_quotas_initialization_on_object_creation(self):
        customer = structure_factories.CustomerFactory()

        for quota_name in self.customer_quotas_names:
            self.assertTrue(customer.quotas.filter(name=quota_name).exists(),
                            'Quota with name "%s" was not added to customer on creation' % quota_name)


class CounterQuotaFieldTest(TestCase):

    def test_target_model_instance_creation_increases_scope_counter_quota(self):
        customer = structure_factories.CustomerFactory()
        structure_factories.ProjectFactory(customer=customer)

        self.assertEqual(customer.quotas.get(name='nc_project_count').usage, 1)

    def test_target_model_instance_deletion_decreases_scope_counter_quota(self):
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)
        project.delete()

        self.assertEqual(customer.quotas.get(name='nc_project_count').usage, 0)
