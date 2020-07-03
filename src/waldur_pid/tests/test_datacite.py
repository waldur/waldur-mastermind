import mock
import requests
from rest_framework import test

from .. import backend
from . import factories


class DataciteTest(test.APITransactionTestCase):
    def setUp(self):
        super(DataciteTest, self).setUp()
        self.offering = factories.OfferingFactory()
        self.backend = backend.DataciteBackend()
        self.requests_patcher = mock.patch('waldur_pid.backend.requests')
        self.mock_requests = self.requests_patcher.start()

        self.logger_patcher = mock.patch('waldur_pid.backend.logger')
        self.mock_logger = self.logger_patcher.start()

    def test_create_doi_succeeded(self):
        response = requests.Response()
        response.status_code = 201
        response.json = lambda: {'data': {'id': 'ID'}}
        self.mock_requests.post.return_value = response

        self.assertFalse(self.offering.datacite_doi)
        self.backend.create_doi(self.offering)
        self.assertTrue(self.offering.datacite_doi)

    def test_create_doi_failed(self):
        response = requests.Response()
        response.status_code = 400
        self.mock_requests.post.return_value = response

        self.assertFalse(self.offering.datacite_doi)
        self.backend.create_doi(self.offering)
        self.assertFalse(self.offering.datacite_doi)
        self.mock_logger.error.assert_called_once()

    def test_update_doi_succeeded(self):
        response = requests.Response()
        response.status_code = 200
        self.mock_requests.put.return_value = response
        self.offering.datacite_doi = '10.15159/tf3a-r005'
        self.offering.save()

        self.backend.update_doi(self.offering)
        self.mock_requests.put.assert_called_once()
        self.assertEqual(
            self.mock_requests.put.call_args[0][0],
            'https://example.com/10.15159/tf3a-r005',
        )

    def test_update_doi_failed(self):
        response = requests.Response()
        response.status_code = 400
        self.mock_requests.post.return_value = response

        self.backend.update_doi(self.offering)
        self.mock_logger.error.assert_called_once()
