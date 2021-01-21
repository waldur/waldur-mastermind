from unittest import mock

from waldur_waldur.tests.helpers import (
    VALID_CUSTOMERS,
    VALID_OFFERINGS,
    RemoteWaldurTestTemplate,
    get_url,
)


class RemoteWaldurApiTest(RemoteWaldurTestTemplate):
    def setUp(self) -> None:
        super(RemoteWaldurApiTest, self).setUp()
        self.request_data = {
            'name': self.service_settings.name,
            'type': self.service_settings.type,
            'token': self.service_settings.token,
            'backend_url': self.service_settings.backend_url,
        }
        self.client.force_authenticate(self.fixture.user)

    @mock.patch('waldur_waldur.backend.WaldurBackend.get_remote_customers')
    def test_remote_walur_get_remote_customers(self, get_remote_customers):
        get_remote_customers.return_value = VALID_CUSTOMERS
        url = get_url(action='remote_customers')
        response = self.client.post(url, data=self.request_data)
        self.assertEqual(200, response.status_code)
        self.assertEqual(VALID_CUSTOMERS, response.data)

    @mock.patch('waldur_waldur.backend.WaldurBackend.get_importable_offerings')
    def test_remote_walur_get_importable_offerings(self, get_importable_offerings):
        get_importable_offerings.return_value = VALID_OFFERINGS
        url = get_url(action='shared_offerings', customer_uuid='1')
        response = self.client.post(url, data=self.request_data)
        self.assertEqual(200, response.status_code)
        self.assertEqual(VALID_OFFERINGS, response.data)

    @mock.patch('waldur_waldur.backend.WaldurBackend.get_importable_offerings')
    def test_remote_walur_get_importable_offerings_with_missing_query_param(
        self, get_importable_offerings
    ):
        get_importable_offerings.return_value = VALID_OFFERINGS
        url = get_url(action='shared_offerings')
        response = self.client.post(url, data=self.request_data)
        self.assertEqual(400, response.status_code)
        self.assertIn('customer_uuid', response.data['url'])
