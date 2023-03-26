from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_vmware import backend, models

from . import factories


class DatastoreGetTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = ProjectFixture()
        self.fixture_2 = ProjectFixture()
        datastore_1 = factories.DatastoreFactory()
        datastore_2 = factories.DatastoreFactory()
        datastore_3 = factories.DatastoreFactory()
        datastore_4 = factories.DatastoreFactory()

        factories.CustomerDatastoreFactory(
            datastore=datastore_1,
            customer=self.fixture.customer,
        )
        factories.CustomerDatastoreFactory(
            datastore=datastore_2,
            customer=self.fixture.customer,
        )
        factories.CustomerDatastoreFactory(
            datastore=datastore_3,
            customer=self.fixture_2.customer,
        )
        factories.CustomerDatastoreFactory(
            datastore=datastore_4,
            customer=self.fixture_2.customer,
        )
        self.url = factories.DatastoreFactory.get_list_url()

    def test_get_datastore_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 4)

    def test_filter_datastore_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {'customer_uuid': self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 2)


class DatastorePullTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.settings = factories.VMwareServiceSettingsFactory()
        self.backend = backend.VMwareBackend(self.settings)
        self.patcher = mock.patch('waldur_vmware.backend.VMwareClient')
        self.mock_client = self.patcher.start()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_delete_old_datastores(self):
        factories.DatastoreFactory(settings=self.settings)
        factories.DatastoreFactory(settings=self.settings)
        self.backend.pull_datastores()
        self.assertEqual(models.Datastore.objects.count(), 0)

    def test_add_new_datastores(self):
        client = mock.MagicMock()
        self.mock_client.return_value = client
        client.list_datastores.return_value = self._generate_datastores()

        self.backend.pull_datastores()
        self.assertEqual(models.Datastore.objects.count(), 1)

    def _generate_datastores(self, count=1):
        datastores = []
        for i in range(count):
            backend_datastore = {
                'name': 'datastore_%s' % i,
                'type': 'VMFS',
                'datastore': 'datastore_%s' % i,
                'capacity': i * 1024 * 1024 * 10,
                'free_space': i * 1024 * 1024 * 5,
            }
            datastores.append(backend_datastore)

        return datastores
