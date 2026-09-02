from rest_framework import status, test

from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


class DatastoreGetTest(test.APITestCase):
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
        self.assertEqual(len(response.data), 4)

    def test_filter_datastore_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
