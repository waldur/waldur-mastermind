from django.test import TransactionTestCase
from reversion.models import Version

from waldur_core.core.utils import silent_call

from . import models as test_models


class TestQuotaField(TransactionTestCase):

    def test_quota_is_automatically_created_with_scope(self):
        scope = test_models.GrandparentModel.objects.create()
        self.assertTrue(scope.quotas.filter(name=test_models.GrandparentModel.Quotas.regular_quota).exists())

    def test_quota_limit_field_create(self):
        child = test_models.GrandparentModel.objects.create(regular_quota=7)
        self.assertEqual(child.quotas.get(name='regular_quota').limit, 7)

    def test_quota_limit_field_update(self):
        child = test_models.GrandparentModel.objects.create()
        child.regular_quota = 9
        child.save()
        self.assertEqual(child.quotas.get(name='regular_quota').limit, 9)

    # XXX: Ideally this method should belong to ReversionMixin tests and
    #      should be separated into several smaller tests.
    def test_quota_versions(self):
        scope = test_models.GrandparentModel.objects.create()
        quota = scope.quotas.get(name=test_models.GrandparentModel.Quotas.regular_quota)
        quota.usage = 13.0
        quota.save()
        # make sure that new version was created after quota usage change.
        latest_version = Version.objects.get_for_object(quota).latest('revision__date_created')
        self.assertEqual(latest_version._object_version.object.usage, quota.usage)
        # make sure that new version was not created if object was saved without data change.
        quota.usage = 13
        quota.save()
        new_latest_version = Version.objects.get_for_object(quota).latest('revision__date_created')
        self.assertEqual(new_latest_version, latest_version)


class TestCounterQuotaField(TransactionTestCase):

    def setUp(self):
        self.grandparent = test_models.GrandparentModel.objects.create()
        self.parent = test_models.ParentModel.objects.create(parent=self.grandparent)
        self.child = test_models.ChildModel.objects.create(parent=self.parent)
        self.quota_field = test_models.ParentModel.Quotas.counter_quota

    def test_counter_quota_usage_is_increased_on_child_creation(self):
        quota = self.parent.quotas.get(name=self.quota_field)
        self.assertEqual(quota.usage, 1)

    def test_counter_quota_usage_is_decreased_on_child_deletion(self):
        self.child.delete()
        quota = self.parent.quotas.get(name=self.quota_field)
        self.assertEqual(quota.usage, 0)

    def test_counter_quota_usage_is_right_after_recalculation(self):
        quota = self.parent.quotas.get(name=self.quota_field)
        quota.usage = 3
        quota.save()

        silent_call('recalculatequotas')

        quota = self.parent.quotas.get(name=self.quota_field)
        self.assertEqual(quota.usage, 1)

    def test_counter_quota_usage_is_working_with_two_models_as_targets(self):
        self.parent.second_children.create()

        quota = self.parent.quotas.get(name=test_models.ParentModel.Quotas.two_targets_counter_quota)
        self.assertEqual(quota.usage, 2)

    def test_delta_quota_usage_is_increased_on_child_creation(self):
        quota = self.parent.quotas.get(name=test_models.ParentModel.Quotas.delta_quota)
        self.assertEqual(quota.usage, 10)

    def test_delta_quota_usage_is_decreased_on_child_deletion(self):
        self.child.delete()
        quota = self.parent.quotas.get(name=test_models.ParentModel.Quotas.delta_quota)
        self.assertEqual(quota.usage, 0)


class TestTotalQuotaField(TransactionTestCase):

    def setUp(self):
        self.grandparent = test_models.GrandparentModel.objects.create()
        self.parent = test_models.ParentModel.objects.create(parent=self.grandparent)
        self.child = test_models.SecondChildModel.objects.create(parent=self.parent, size=100)
        self.quota_field = test_models.ParentModel.Quotas.total_quota

    def test_counter_quota_usage_is_increased_on_child_creation(self):
        quota = self.parent.quotas.get(name=self.quota_field)
        self.assertEqual(quota.usage, 100)

    def test_counter_quota_usage_is_decreased_on_child_deletion(self):
        self.child.delete()
        quota = self.parent.quotas.get(name=self.quota_field)
        self.assertEqual(quota.usage, 0)

    def test_counter_quota_usage_is_right_after_recalculation(self):
        quota = self.parent.quotas.get(name=self.quota_field)
        quota.usage = 0
        quota.save()

        silent_call('recalculatequotas')

        quota = self.parent.quotas.get(name=self.quota_field)
        self.assertEqual(quota.usage, 100)


class TestUsageAggregatorField(TransactionTestCase):

    def setUp(self):
        self.grandparent = test_models.GrandparentModel.objects.create()
        self.parents = [test_models.ParentModel.objects.create(parent=self.grandparent) for _ in range(2)]
        non_quota_parent = test_models.NonQuotaParentModel.objects.create()
        self.children = [test_models.ChildModel.objects.create(parent=parent, non_quota_parent=non_quota_parent)
                         for parent in self.parents]

        self.child_quota_field = test_models.ChildModel.Quotas.usage_aggregator_quota
        self.parent_quota_field = test_models.ParentModel.Quotas.usage_aggregator_quota
        self.grandparent_quota_field = test_models.GrandparentModel.Quotas.usage_aggregator_quota

    def test_aggregator_usage_increases_on_child_quota_usage_increase(self):
        usage_value = 10
        for child in self.children:
            quota = child.quotas.get(name=self.child_quota_field)
            quota.usage = usage_value
            quota.save()

        for parent in self.parents:
            quota = parent.quotas.get(name=self.parent_quota_field)
            self.assertEqual(quota.usage, usage_value)

        quota = self.grandparent.quotas.get(name=self.grandparent_quota_field)
        self.assertEqual(quota.usage, usage_value * len(self.children))

    def test_aggregator_usage_decreases_on_child_deletion(self):
        usage_value = 10
        for child in self.children:
            quota = child.quotas.get(name=self.child_quota_field)
            quota.usage = usage_value
            quota.save()

        first_child = self.children[0]
        first_child.delete()
        quota = first_child.parent.quotas.get(name=self.parent_quota_field)
        self.assertEqual(quota.usage, 0)

        quota = self.grandparent.quotas.get(name=self.grandparent_quota_field)
        self.assertEqual(quota.usage, usage_value)

    def test_aggregator_usage_increases_on_child_creation(self):
        usage_value = 10
        test_models.ChildModel.Quotas.usage_aggregator_quota.default_usage = usage_value

        parent = self.parents[0]
        test_models.ChildModel.objects.create(parent=parent)

        quota = parent.quotas.get(name=self.parent_quota_field)
        self.assertEqual(quota.usage, usage_value)
        quota = self.grandparent.quotas.get(name=self.parent_quota_field)
        self.assertEqual(quota.usage, usage_value)

    def test_usage_aggregator_recalculation(self):
        usage_value = 10
        for child in self.children:
            quota = child.quotas.get(name=self.child_quota_field)
            quota.usage = usage_value
            quota.save()
        # set quota as wrong number to test recalculation
        for parent in self.parents:
            parent.set_quota_usage(self.parent_quota_field, 666)
        self.grandparent.set_quota_usage(self.grandparent_quota_field, 1232)

        silent_call('recalculatequotas')

        for parent in self.parents:
            quota = parent.quotas.get(name=self.parent_quota_field)
            self.assertEqual(quota.usage, usage_value)

        quota = self.grandparent.quotas.get(name=self.grandparent_quota_field)
        self.assertEqual(quota.usage, usage_value * len(self.children))

    def test_usage_aggregator_quota_works_with_specified_child_quota_name(self):
        usage_value = 10
        for child in self.children:
            quota = child.quotas.get(name=self.child_quota_field)
            quota.usage = usage_value
            quota.save()

        # second_usage_aggregator_quota quota should increases too
        for parent in self.parents:
            quota = parent.quotas.get(name=test_models.ParentModel.Quotas.second_usage_aggregator_quota)
            self.assertEqual(quota.usage, usage_value)


class TestLimitAggregatorField(TransactionTestCase):

    def setUp(self):
        self.grandparent = test_models.GrandparentModel.objects.create()
        self.parents = [test_models.ParentModel.objects.create(parent=self.grandparent) for _ in range(2)]
        non_quota_parent = test_models.NonQuotaParentModel.objects.create()
        self.children = [test_models.ChildModel.objects.create(parent=parent, non_quota_parent=non_quota_parent)
                         for parent in self.parents]

        self.child_quota_field = test_models.ChildModel.Quotas.limit_aggregator_quota
        self.parent_quota_field = test_models.ParentModel.Quotas.limit_aggregator_quota
        self.grandparent_quota_field = test_models.GrandparentModel.Quotas.limit_aggregator_quota

    def test_aggregator_usage_increases_on_child_quota_limit_increase(self):
        limit_value = 10
        for child in self.children:
            quota = child.quotas.get(name=self.child_quota_field)
            quota.limit = limit_value
            quota.save()

        for parent in self.parents:
            quota = parent.quotas.get(name=self.parent_quota_field)
            self.assertEqual(quota.usage, limit_value)

        quota = self.grandparent.quotas.get(name=self.grandparent_quota_field)
        self.assertEqual(quota.usage, limit_value * len(self.children))

    def test_aggregator_usage_decreases_on_child_deletion(self):
        limit_value = 10
        for child in self.children:
            quota = child.quotas.get(name=self.child_quota_field)
            quota.limit = limit_value
            quota.save()

        first_child = self.children[0]
        first_child.delete()
        quota = first_child.parent.quotas.get(name=self.parent_quota_field)
        self.assertEqual(quota.usage, 0)

        quota = self.grandparent.quotas.get(name=self.grandparent_quota_field)
        self.assertEqual(quota.usage, limit_value)

    def test_aggregator_usage_increases_on_child_creation(self):
        limit_value = 10
        test_models.ChildModel.Quotas.limit_aggregator_quota.default_limit = limit_value

        parent = self.parents[0]
        test_models.ChildModel.objects.create(parent=parent)

        quota = parent.quotas.get(name=self.parent_quota_field)
        self.assertEqual(quota.usage, limit_value)
        quota = self.grandparent.quotas.get(name=self.grandparent_quota_field)
        self.assertEqual(quota.usage, limit_value)

    def test_limit_aggregator_recalculation(self):
        limit_value = 10
        for child in self.children:
            quota = child.quotas.get(name=self.child_quota_field)
            quota.limit = limit_value
            quota.save()
        # set quota as wrong number to test recalculation
        for parent in self.parents:
            parent.set_quota_limit(self.parent_quota_field, 666)
        self.grandparent.set_quota_limit(self.grandparent_quota_field, 1232)

        silent_call('recalculatequotas')

        for parent in self.parents:
            quota = parent.quotas.get(name=self.parent_quota_field)
            self.assertEqual(quota.usage, limit_value)

        quota = self.grandparent.quotas.get(name=self.grandparent_quota_field)
        self.assertEqual(quota.usage, limit_value * len(self.children))
