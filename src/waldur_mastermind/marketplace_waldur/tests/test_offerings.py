from unittest import mock

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ServiceFixture
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests.test_offerings import OfferingCreateTest
from waldur_waldur.apps import RemoteWaldurConfig
from waldur_waldur.tests.helpers import VALID_CUSTOMERS, VALID_OFFERINGS, get_url


class RemoteOfferingCreateTest(OfferingCreateTest):
    def setUp(self):
        super(RemoteOfferingCreateTest, self).setUp()
        self.service_fixture = ServiceFixture()
        self.service_settings = self.service_fixture.service_settings
        self.service_settings.token = 'abc123'
        self.service_settings.backend_url = 'https://remote.waldur.example.com/api/'
        self.service_settings.type = RemoteWaldurConfig.service_name
        self.service_settings.save()
        self.request_data = {
            'name': self.service_settings.name,
            'type': self.service_settings.type,
            'token': self.service_settings.token,
            'backend_url': self.service_settings.backend_url,
        }

        self.patch_list_remote_customers = mock.patch(
            'waldur_waldur.client.WaldurClient.list_remote_customers'
        )
        self.patch_list_public_offerings = mock.patch(
            'waldur_waldur.client.WaldurClient.list_public_offerings'
        )

        self.list_remote_customers = self.patch_list_remote_customers.start()
        self.list_public_offerings = self.patch_list_public_offerings.start()

        self.list_remote_customers.return_value = VALID_CUSTOMERS
        self.list_public_offerings.return_value = VALID_OFFERINGS

        self.addCleanup(self.patch_list_remote_customers.stop)
        self.addCleanup(self.patch_list_public_offerings.stop)

    def create_offering(self, user, attributes=False, add_payload=None):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = marketplace_factories.OfferingFactory.get_list_url()
        self.provider = marketplace_factories.ServiceProviderFactory(
            customer=self.customer
        )

        remote_customers_url = get_url('remote_customers')
        customers_response = self.client.post(
            remote_customers_url, data=self.request_data
        )
        customers_list = customers_response.data

        shared_offerings_url = get_url(
            'shared_offerings', customer_uuid=customers_list[0]['uuid']
        )
        offerings_response = self.client.post(
            shared_offerings_url, data=self.request_data
        )
        offerings_list = offerings_response.data

        selected_offering = offerings_list[0]

        payload = {
            'name': selected_offering['name'],
            'category': marketplace_factories.CategoryFactory.get_url(),
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
            'type': 'Waldur.RemoteOffering',
            'plans': selected_offering['plans'],
        }

        if attributes:
            payload['attributes'] = selected_offering['attributes']

        if add_payload:
            payload.update(add_payload)

        return self.client.post(url, payload)
