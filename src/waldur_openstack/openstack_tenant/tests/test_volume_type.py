from ddt import data, ddt
from rest_framework import status, test

from waldur_core.quotas.models import QuotaLimit
from waldur_openstack.openstack_tenant.tests import factories, fixtures


@ddt
class VolumeTypeTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.url = factories.VolumeTypeFactory.get_list_url()
        self.fixture.volume

    @data('admin', 'manager', 'staff')
    def test_authorized_users_can_get_volume_type_list(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_unauthorized_users_cannot_get_volume_type_list(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_quota_for_volume_type_is_propagated_from_tenant_to_private_settings(self):
        self.fixture.tenant.set_quota_limit('gigabytes_ssd', 100)
        scope = self.fixture.openstack_tenant_service_settings
        self.assertEqual(scope.get_quota_limit('gigabytes_ssd'), 100)

    def test_quota_for_volume_type_is_deleted_from_private_settings(self):
        self.fixture.tenant.set_quota_limit('gigabytes_ssd', 100)
        QuotaLimit.objects.get(scope=self.fixture.tenant, name='gigabytes_ssd').delete()
        self.assertFalse(
            QuotaLimit.objects.filter(
                scope=self.fixture.openstack_tenant_service_settings,
                name='gigabytes_ssd',
            ).exists()
        )
