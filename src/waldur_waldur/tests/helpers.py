from django.urls import reverse
from rest_framework.test import APITransactionTestCase

from waldur_core.structure.tests.fixtures import ServiceFixture
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_waldur.apps import RemoteWaldurConfig

VALID_OFFERINGS = [
    {
        'name': 'HPC%s' % i,
        'uuid': str(i),
        'category_title': 'HPC',
        'type': 'SlurmInvoices.SlurmPackage',
        'customer_name': 'Customer1',
        'customer_uuid': '1',
        'plans': [{'name': 'Small', 'unit': UnitPriceMixin.Units.PER_MONTH,}],
        'attributes': {
            'cloudDeploymentModel': 'private_cloud',
            'vendorType': 'reseller',
            'userSupportOptions': ['web_chat', 'phone'],
            'dataProtectionInternal': 'ipsec',
            'dataProtectionExternal': 'tls12',
        },
    }
    for i in range(1, 7)
]

VALID_CUSTOMERS = [
    {'name': 'Customer %s' % item, 'uuid': str(item)} for item in range(1, 4)
]


def get_url(action=None, customer_uuid=None):
    url = reverse('remote-waldur-api-list')
    if action:
        url += action + '/'
    if customer_uuid:
        url += '?customer_uuid=%s' % customer_uuid
    return url


class RemoteWaldurTestTemplate(APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = ServiceFixture()
        self.service_settings = self.fixture.service_settings
        self.service_settings.token = 'abc123'
        self.service_settings.backend_url = 'https://remote.waldur.example.com/api/'
        self.service_settings.type = RemoteWaldurConfig.service_name
        self.service_settings.save()
