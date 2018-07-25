from datetime import timedelta
from ddt import ddt, data
from django.utils import timezone
from rest_framework import test, status
from reversion.models import Version

from waldur_core.core import utils as core_utils
from waldur_core.quotas.tests import factories
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import (factories as structure_factories,
                                         fixtures as structure_fixtures)


@ddt
class QuotaUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        super(QuotaUpdateTest, self).setUp()
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


class QuotaHistoryTest(test.APITransactionTestCase):

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.owner = structure_factories.UserFactory(username='owner')
        self.customer.add_user(self.owner, structure_models.CustomerRole.OWNER)

        self.quota = factories.QuotaFactory(scope=self.customer)
        self.url = factories.QuotaFactory.get_url(self.quota, 'history')
        # Hook for test: lets say that revision was created one hour ago
        version = Version.objects.get_for_object(self.quota).filter(revision__date_created__lte=timezone.now()).first()
        version.revision.date_created = timezone.now() - timedelta(hours=1)
        version.revision.save()

    def test_old_version_of_quota_is_available(self):
        old_usage = self.quota.usage
        self.quota.usage = self.quota.usage + 1
        self.quota.save()
        history_timestamp = core_utils.datetime_to_timestamp(timezone.now() - timedelta(minutes=30))

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url, data={'point': history_timestamp})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['point'], history_timestamp)
        self.assertEqual(response.data[0]['object']['usage'], old_usage)

    def test_endpoint_does_not_return_object_if_date(self):
        history_timestamp = core_utils.datetime_to_timestamp(timezone.now() - timedelta(hours=2))

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url, data={'point': history_timestamp})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('object', response.data[0])

    def test_timeinterval_is_splitted_equal_parts(self):
        start_timestamp = 1436094000
        end_timestamp = 1436096000

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url, data={'points_count': 3, 'start': start_timestamp, 'end': end_timestamp})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['point'], start_timestamp)
        self.assertEqual(response.data[1]['point'], start_timestamp + (end_timestamp - start_timestamp) / 2)
        self.assertEqual(response.data[2]['point'], end_timestamp)


# TODO: add CRUD tests for quota endpoint.
