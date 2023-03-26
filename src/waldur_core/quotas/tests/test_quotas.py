from ddt import data, ddt
from rest_framework import status, test

from waldur_core.quotas.tests import factories
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import fixtures as structure_fixtures


@ddt
class QuotaUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = structure_fixtures.ServiceFixture()
        self.quota_name = structure_models.Customer.Quotas.nc_project_count
        self.quota = self.fixture.customer.quotas.get(name=self.quota_name)
        self.quota.usage = 10
        self.quota.save()
        self.url = factories.QuotaFactory.get_url(self.quota)

    def test_staff_can_set_quota_limit(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.put(self.url, {'limit': self.quota.usage + 1})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['limit'], self.quota.usage + 1)

    @data('global_support', 'owner')
    def test_other_users_can_not_set_quota_limit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.put(self.url, {'limit': self.quota.usage + 1})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_quota_limit_should_not_be_less_than_usage(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.put(self.url, {'limit': self.quota.usage - 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
