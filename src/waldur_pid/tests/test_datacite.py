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
