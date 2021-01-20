from unittest import mock

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_waldur.backend import WaldurBackend
from waldur_waldur.exceptions import WaldurClientException
from waldur_waldur.tests.helpers import (
    VALID_CUSTOMERS,
    VALID_OFFERINGS,
    RemoteWaldurTestTemplate,
)


class RemoteWaldurBackendTest(RemoteWaldurTestTemplate):
    def setUp(self) -> None:
        super(RemoteWaldurBackendTest, self).setUp()
        self.backend = WaldurBackend(self.service_settings)

    @mock.patch('waldur_waldur.client.WaldurClient.list_public_offerings')
    def test_get_public_offerings_from_remote_waldur(self, list_public_offerings):
        list_public_offerings.return_value = VALID_OFFERINGS
        offerings_json = self.backend.get_shared_offerings()
        self.assertEqual(6, len(offerings_json))
        self.assertEqual(
            VALID_OFFERINGS, offerings_json,
        )

    @mock.patch('waldur_waldur.client.WaldurClient.list_public_offerings')
    def test_importable_offerings_list_excludes_already_imported_ones(
        self, list_public_offerings
    ):
        offering: marketplace_models.Offering = marketplace_fixtures.MarketplaceFixture().offering
        offering.scope = self.service_settings
        offering.backend_id = '1'
        offering.save()
        customer = offering.customer

        valid_offerings = []
        for offering in VALID_OFFERINGS:
            offering_copy = offering.copy()
            offering_copy['customer_uuid'] = customer.uuid
            valid_offerings.append(offering_copy)

        list_public_offerings.return_value = valid_offerings

        offerings_json = self.backend.get_importable_offerings()
        self.assertEqual(5, len(offerings_json))
        self.assertNotIn('1', [item['uuid'] for item in offerings_json])

    @mock.patch('waldur_waldur.client.WaldurClient.list_remote_customers')
    def test_get_customers_from_remote_waldur(self, list_remote_customers):
        list_remote_customers.return_value = VALID_CUSTOMERS
        customers_json = self.backend.get_remote_customers()
        self.assertEqual(3, len(customers_json))
        self.assertEqual(VALID_CUSTOMERS, customers_json)

    @mock.patch('waldur_waldur.client.WaldurClient.ping')
    def test_ping_remote_waldur(self, ping):
        ping.return_value = None
        self.assertTrue(self.backend.ping())

    @mock.patch('waldur_waldur.client.WaldurClient.ping')
    def test_ping_remote_waldur_raises_exception(self, ping):
        ping.side_effect = WaldurClientException()
        self.assertFalse(self.backend.ping())
