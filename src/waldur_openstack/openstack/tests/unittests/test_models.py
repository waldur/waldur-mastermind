from ddt import ddt, data
from django.test import TestCase

from .. import factories


@ddt
class TenantTest(TestCase):
    def setUp(self):
        self.tenant = factories.TenantFactory()

    def test_quota_format_returns_integer_for_vcpu_quota(self):
        result = self.tenant.format_quota(self.tenant.Quotas.vcpu.name, 10.0)

        self.assertEqual(result, 10)

    @data('ram', 'storage')
    def test_quota_format_returns_units_for_storage_quotas(self, name):
        result = self.tenant.format_quota(name, 15 * 1024)

        self.assertEqual(result, '15 GB')
