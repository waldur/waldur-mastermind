from unittest import mock

from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import fixtures


class OfferingGeoCodeTest(test.APITransactionTestCase):
    def setUp(self):
        self.url = reverse("geocode")
        self.fixture = fixtures.UserFixture()

        mock_patch = mock.patch("waldur_core.core.utils.Nominatim")
        mock_nominatim = mock_patch.start()

        class Location:
            latitude = 10.0
            longitude = 10.0

        mock_nominatim().geocode.return_value = Location()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_get_lat_lon_from_address(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"address": "Address"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"latitude": 10.0, "longitude": 10.0})
