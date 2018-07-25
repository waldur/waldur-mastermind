import mock
import httplib
import unittest

from libcloud.common.types import LibcloudError

from ..driver import AzureResponse


@unittest.skip
class ErrorMessageParsingTest(unittest.TestCase):
    def setUp(self):
        self._mock_response = mock.Mock()
        self._mock_response.getheaders.return_value = []
        self._mock_response.status = httplib.OK
        self._mock_response._original_data = None
        self._mock_connection = mock.Mock()

    def test_azure_response_parses_error_message(self):
        self._mock_response.status = httplib.NOT_FOUND
        self._mock_response.read.return_value = '<Error xmlns="http://schemas.microsoft.com/windowsazure" xmlns:i="http://www.w3.org/2001/XMLSchema-instance"><Code>ResourceNotFound</Code><Message>The resource service name hostedservices is not supported.</Message></Error>'

        # response object has been updated in 2.1
        self._mock_response.status_code = self._mock_response.status
        self._mock_response.headers = []
        self._mock_response.text = self._mock_response.read.return_value
        response = AzureResponse(response=self._mock_response,
                                 connection=self._mock_connection)
        try:
            response.parse_error()
        except LibcloudError as e:
            self.assertEqual(e.value, 'ResourceNotFound: The resource service name hostedservices is not supported. Status code: 404.')
        else:
            self.fail('Exception is not thrown')
