from django.test import TestCase

from waldur_core.quotas import exceptions
from waldur_core.quotas.tests.models import GrandparentModel


class QuotaModelMixinTest(TestCase):
    def test_default_quota_is_unlimited(self):
        instance = GrandparentModel.objects.create()
        self.assertEqual(instance.get_quota_limit('regular_quota'), -1)

    def test_quota_with_default_limit(self):
        instance = GrandparentModel.objects.create()
        self.assertEqual(instance.get_quota_limit('quota_with_default_limit'), 100)

    def test_add_usage_validates_with_unlimited_quota(self):
        instance = GrandparentModel.objects.create()
        try:
            instance.add_quota_usage('regular_quota', 10)
        except exceptions.QuotaValidationError:
            self.fail(
                'add_quota_usage should not raise exception if quota is unlimited'
            )

    def test_add_usage_skips_validation_with_limited_quota_but_negative_delta(self):
        instance = GrandparentModel.objects.create()
        try:
            instance.add_quota_usage('quota_with_default_limit', -10)
        except exceptions.QuotaValidationError:
            self.fail('add_quota_usage should not raise exception if delta is negative')

    def test_add_usage_fails_if_quota_is_over_limit(self):
        instance = GrandparentModel.objects.create()
        self.assertRaises(
            exceptions.QuotaValidationError,
            instance.add_quota_usage,
            quota_name='quota_with_default_limit',
            delta=200,
            validate=True,
        )
