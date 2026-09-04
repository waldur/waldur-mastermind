from django.test import TestCase

from waldur_core.quotas import exceptions
from waldur_core.quotas.tests.models import GrandparentModel


class QuotaModelMixinTest(TestCase):
    def test_default_quota_is_unlimited(self):
        instance = GrandparentModel.objects.create()
        self.assertEqual(instance.get_quota_limit("regular_quota"), -1)

    def test_quota_with_default_limit(self):
        instance = GrandparentModel.objects.create()
        self.assertEqual(instance.get_quota_limit("quota_with_default_limit"), 100)

    def test_quota_limits_report_stored_zero_as_zero(self):
        instance = GrandparentModel.objects.create()
        instance.set_quota_limit("regular_quota", 0)
        self.assertEqual(instance.quota_limits["regular_quota"], 0)

    def test_quota_limits_omit_quotas_without_a_row(self):
        instance = GrandparentModel.objects.create()
        self.assertNotIn("regular_quota", instance.quota_limits)

    def test_quotas_report_stored_zero_as_zero(self):
        instance = GrandparentModel.objects.create()
        instance.set_quota_limit("regular_quota", 0)
        quota = self.get_quota(instance, "regular_quota")
        self.assertEqual(quota["limit"], 0)

    def test_quotas_report_missing_limit_as_unlimited(self):
        instance = GrandparentModel.objects.create()
        instance.add_quota_usage("regular_quota", 10)
        quota = self.get_quota(instance, "regular_quota")
        self.assertEqual(quota["limit"], -1)

    def get_quota(self, instance, name):
        return next(quota for quota in instance.quotas if quota["name"] == name)

    def test_add_usage_validates_with_unlimited_quota(self):
        instance = GrandparentModel.objects.create()
        try:
            instance.add_quota_usage("regular_quota", 10)
        except exceptions.QuotaValidationError:
            self.fail(
                "add_quota_usage should not raise exception if quota is unlimited"
            )

    def test_add_usage_skips_validation_with_limited_quota_but_negative_delta(self):
        instance = GrandparentModel.objects.create()
        try:
            instance.add_quota_usage("quota_with_default_limit", -10)
        except exceptions.QuotaValidationError:
            self.fail("add_quota_usage should not raise exception if delta is negative")

    def test_add_usage_fails_if_quota_is_over_limit(self):
        instance = GrandparentModel.objects.create()
        self.assertRaises(
            exceptions.QuotaValidationError,
            instance.add_quota_usage,
            quota_name="quota_with_default_limit",
            delta=200,
            validate=True,
        )
