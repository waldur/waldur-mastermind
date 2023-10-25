from django.test import TransactionTestCase

from waldur_core.core.utils import silent_call
from waldur_core.quotas.tests import models as test_models


class TestCounterQuotaField(TransactionTestCase):
    def setUp(self):
        self.grandparent = test_models.GrandparentModel.objects.create()
        self.parent = test_models.ParentModel.objects.create(parent=self.grandparent)
        self.child = test_models.ChildModel.objects.create(parent=self.parent)
        self.quota_field = test_models.ParentModel.Quotas.counter_quota

    def test_counter_quota_usage_is_increased_on_child_creation(self):
        self.assertEqual(self.parent.get_quota_usage(self.quota_field), 1)

    def test_counter_quota_usage_is_decreased_on_child_deletion(self):
        self.child.delete()
        self.assertEqual(self.parent.get_quota_usage(self.quota_field), 0)

    def test_counter_quota_usage_is_right_after_recalculation(self):
        self.parent.set_quota_usage(self.quota_field, 3)

        silent_call('recalculatequotas')

        usage = self.parent.get_quota_usage(self.quota_field)
        self.assertEqual(usage, 1)

    def test_counter_quota_usage_is_working_with_two_models_as_targets(self):
        self.parent.second_children.create()

        usage = self.parent.get_quota_usage(
            test_models.ParentModel.Quotas.two_targets_counter_quota
        )
        self.assertEqual(usage, 2)

    def test_delta_quota_usage_is_increased_on_child_creation(self):
        usage = self.parent.get_quota_usage(test_models.ParentModel.Quotas.delta_quota)
        self.assertEqual(usage, 10)

    def test_delta_quota_usage_is_decreased_on_child_deletion(self):
        self.child.delete()
        usage = self.parent.get_quota_usage(test_models.ParentModel.Quotas.delta_quota)
        self.assertEqual(usage, 0)


class TestTotalQuotaField(TransactionTestCase):
    def setUp(self):
        self.grandparent = test_models.GrandparentModel.objects.create()
        self.parent = test_models.ParentModel.objects.create(parent=self.grandparent)
        self.child = test_models.SecondChildModel.objects.create(
            parent=self.parent, size=100
        )
        self.quota_field = test_models.ParentModel.Quotas.total_quota

    def test_counter_quota_usage_is_increased_on_child_creation(self):
        usage = self.parent.get_quota_usage(self.quota_field)
        self.assertEqual(usage, 100)

    def test_counter_quota_usage_is_decreased_on_child_deletion(self):
        self.child.delete()
        usage = self.parent.get_quota_usage(self.quota_field)
        self.assertEqual(usage, 0)

    def test_counter_quota_usage_is_right_after_recalculation(self):
        self.parent.set_quota_usage(self.quota_field, 0)

        silent_call('recalculatequotas')

        usage = self.parent.get_quota_usage(self.quota_field)
        self.assertEqual(usage, 100)


class TestUsageAggregatorField(TransactionTestCase):
    def setUp(self):
        self.grandparent = test_models.GrandparentModel.objects.create()
        self.parents = [
            test_models.ParentModel.objects.create(parent=self.grandparent)
            for _ in range(2)
        ]
        non_quota_parent = test_models.NonQuotaParentModel.objects.create()
        self.children = [
            test_models.ChildModel.objects.create(
                parent=parent, non_quota_parent=non_quota_parent
            )
            for parent in self.parents
        ]

    def test_aggregator_usage_increases_on_child_quota_usage_increase(self):
        usage_value = 10
        for child in self.children:
            child.set_quota_usage('usage_aggregator_quota', usage_value)

        for parent in self.parents:
            actual_usage = parent.get_quota_usage('usage_aggregator_quota')
            self.assertEqual(actual_usage, usage_value)

        actual_usage = self.grandparent.get_quota_usage('usage_aggregator_quota')
        self.assertEqual(actual_usage, usage_value * len(self.children))

    def test_aggregator_usage_decreases_on_child_deletion(self):
        usage_value = 10
        for child in self.children:
            child.set_quota_usage('usage_aggregator_quota', usage_value)

        first_child = self.children[0]
        first_child.delete()
        actual_usage = first_child.parent.get_quota_usage('usage_aggregator_quota')
        self.assertEqual(actual_usage, 0)

        actual_usage = self.grandparent.get_quota_usage('usage_aggregator_quota')
        self.assertEqual(actual_usage, usage_value)

    def test_usage_aggregator_recalculation(self):
        usage_value = 10
        for child in self.children:
            child.set_quota_usage('usage_aggregator_quota', usage_value)
        # set quota as wrong number to test recalculation
        for parent in self.parents:
            parent.set_quota_usage('usage_aggregator_quota', 666)
        self.grandparent.set_quota_usage('usage_aggregator_quota', 1232)

        silent_call('recalculatequotas')

        for parent in self.parents:
            actual_usage = parent.get_quota_usage('usage_aggregator_quota')
            self.assertEqual(actual_usage, usage_value)

        actual_usage = self.grandparent.get_quota_usage('usage_aggregator_quota')
        self.assertEqual(actual_usage, usage_value * len(self.children))

    def test_usage_aggregator_quota_works_with_specified_child_quota_name(self):
        usage_value = 10
        for child in self.children:
            child.set_quota_usage('usage_aggregator_quota', usage_value)

        # second_usage_aggregator_quota quota should increases too
        for parent in self.parents:
            actual_usage = parent.get_quota_usage(
                test_models.ParentModel.Quotas.second_usage_aggregator_quota
            )
            self.assertEqual(actual_usage, usage_value)
