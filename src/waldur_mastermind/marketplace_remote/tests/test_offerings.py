import responses
from rest_framework import status, test

from waldur_core.structure.tests.factories import UserFactory


class RemoteCustomersTest(test.APITransactionTestCase):
    @responses.activate
    def test_remote_customers_are_listed_for_given_token_and_api_url(self):
        responses.add(responses.GET, 'https://remote-waldur.com/customers/', json=[])
        self.client.force_login(UserFactory())
        response = self.client.post(
            '/api/remote-waldur-api/remote_customers/',
            {'api_url': 'https://remote-waldur.com/', 'token': 'valid_token',},
        )
        self.assertEqual(
            responses.calls[0].request.headers['Authorization'], 'token valid_token'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])
