from ddt import data, ddt
from rest_framework import status, test

from . import factories, fixtures


@ddt
class VolumeAvailabilityZoneTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.url = factories.VolumeAvailabilityZoneFactory.get_list_url()
        self.fixture.volume

    @data('admin', 'manager', 'staff')
    def test_authorized_users_can_get_volume_availability_zone_list(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_unauthorized_users_cannot_get_volume_availability_zone_list(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
