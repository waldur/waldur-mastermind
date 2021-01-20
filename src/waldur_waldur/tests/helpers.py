from rest_framework.test import APITransactionTestCase

from waldur_core.structure.tests.fixtures import ServiceFixture
from waldur_waldur.apps import RemoteWaldurConfig

VALID_OFFERINGS = [
    {
        'name': 'HPC1',
        'uuid': '1',
        'category_title': 'HPC',
        'type': 'SlurmInvoices.SlurmPackage',
        'customer_name': 'Customer1',
        'customer_uuid': '1',
    },
    {
        'name': 'HPC2',
        'uuid': '2',
        'category_title': 'Private clouds',
        'type': 'Packages.Template',
        'customer_name': 'Customer1',
        'customer_uuid': '1',
    },
    {
        'name': 'HPC3',
        'uuid': '3',
        'category_title': 'Private clouds',
        'type': 'Packages.Template',
        'customer_name': 'Customer1',
        'customer_uuid': '1',
    },
    {
        'name': 'HPC4',
        'uuid': '4',
        'category_title': 'Private clouds',
        'type': 'Packages.Template',
        'customer_name': 'Customer1',
        'customer_uuid': '1',
    },
    {
        'name': 'Kubernetes1',
        'uuid': '5',
        'category_title': 'Platform',
        'type': 'Marketplace.Rancher',
        'customer_name': 'Customer1',
        'customer_uuid': '1',
    },
    {
        'name': 'HPC5',
        'uuid': '6',
        'category_title': 'HPC',
        'type': 'SlurmInvoices.SlurmPackage',
        'customer_name': 'Customer1',
        'customer_uuid': '1',
    },
]

VALID_CUSTOMERS = [
    {'name': 'Customer %s' % item, 'uuid': str(item)} for item in range(1, 4)
]


class RemoteWaldurTestTemplate(APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = ServiceFixture()
        self.service_settings = self.fixture.service_settings
        self.service_settings.token = 'abc123'
        self.service_settings.backend_url = 'https://remote.waldur.example.com/api/'
        self.service_settings.type = RemoteWaldurConfig.service_name
        self.service_settings.save()
