import random

from django.test import TestCase

from ..models import GrandparentModel
from ... import exceptions


class QuotaModelMixinTest(TestCase):

    def test_default_quota_is_unlimited(self):
        instance = GrandparentModel.objects.create()
        self.assertEqual(instance.quotas.get(name='regular_quota').limit, -1)

    def test_quota_with_default_limit(self):
        instance = GrandparentModel.objects.create()
        self.assertEqual(instance.quotas.get(name='quota_with_default_limit').limit, 100)

    def test_add_usage_validates_with_unlimited_quota(self):
        instance = GrandparentModel.objects.create()
        try:
            instance.add_quota_usage('regular_quota', 10, validate=True)
        except exceptions.QuotaValidationError:
            self.fail('add_quota_usage should not raise exception if quota is unlimited')

    def test_add_usage_skips_validation_with_limited_quota_but_negative_delta(self):
        instance = GrandparentModel.objects.create()
        try:
            instance.add_quota_usage('quota_with_default_limit', -10, validate=True)
        except exceptions.QuotaValidationError:
            self.fail('add_quota_usage should not raise exception if delta is negative')

    def test_add_usage_fails_if_quota_is_over_limit(self):
        instance = GrandparentModel.objects.create()
        self.assertRaises(exceptions.QuotaValidationError,
                          instance.add_quota_usage,
                          quota_name='quota_with_default_limit',
                          usage_delta=200,
                          validate=True)

    def test_quotas_sum_calculation_if_all_values_are_positive(self):
        # we have 3 memberships:
        instances = [GrandparentModel.objects.create() for _ in range(3)]

        # each membership has non zero quotas:
        for instance in instances:
            for quota_name in instance.get_quotas_names():
                limit = random.choice([10, 20, 30, 40])
                instance.set_quota_limit(quota_name, limit)
                instance.set_quota_usage(quota_name, limit / 2)
        owners = instances[:2]

        sum_of_quotas = GrandparentModel.get_sum_of_quotas_as_dict(owners)

        expected_sum_of_quotas = {}
        for quota_name in GrandparentModel.get_quotas_names():
            expected_sum_of_quotas[quota_name] = sum(owner.quotas.get(name=quota_name).limit for owner in owners)
            expected_sum_of_quotas[quota_name + '_usage'] = sum(
                owner.quotas.get(name=quota_name).usage for owner in owners)

        self.assertEqual(expected_sum_of_quotas, sum_of_quotas)

    def test_quotas_sum_calculation_if_some_limit_is_negative(self):
        instances = [GrandparentModel.objects.create() for _ in range(3)]
        instances[0].set_quota_limit('regular_quota', -1)
        instances[1].set_quota_limit('regular_quota', 10)
        instances[2].set_quota_limit('regular_quota', 30)

        sum_of_quotas = GrandparentModel.get_sum_of_quotas_as_dict(
            instances, quota_names=['regular_quota'], fields=['limit'])
        self.assertEqual({'regular_quota': -1}, sum_of_quotas)

    def test_quotas_sum_calculation_if_all_limits_are_negative(self):
        instances = [GrandparentModel.objects.create() for _ in range(3)]
        instances[0].set_quota_limit('regular_quota', -1)
        instances[1].set_quota_limit('regular_quota', -1)
        instances[2].set_quota_limit('regular_quota', -1)

        sum_of_quotas = GrandparentModel.get_sum_of_quotas_as_dict(
            instances, quota_names=['regular_quota'], fields=['limit'])
        self.assertEqual({'regular_quota': -1}, sum_of_quotas)
