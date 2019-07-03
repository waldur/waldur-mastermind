from rest_framework import status, test

from . import factories, fixtures


class VirtualMachineCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.fixture.spl
        self.fixture.customer_cluster
        self.url = factories.VirtualMachineFactory.get_list_url()

    def test_create_vm(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_cluster_settings_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        payload['cluster'] = factories.ClusterFactory.get_url()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data[0], 'This cluster is not available for this service.')

    def test_cluster_customer_validation(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.get_valid_payload()
        cluster = factories.ClusterFactory(settings=self.fixture.settings)
        payload['cluster'] = factories.ClusterFactory.get_url(cluster)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data[0], 'This cluster is not available for this customer.')

    def get_valid_payload(self):
        return {
            'name': 'VMware VM',
            'service_project_link': factories.VMwareServiceProjectLinkFactory.get_url(self.fixture.spl),
            'template': factories.TemplateFactory.get_url(self.fixture.template),
            'cluster': factories.ClusterFactory.get_url(self.fixture.cluster),
        }
