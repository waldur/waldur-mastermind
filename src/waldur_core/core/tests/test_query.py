from rest_framework import status
from rest_framework.test import APITransactionTestCase

from waldur_core.structure.tests import fixtures


class QueryTest(APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.url = "/api/query/"

    def test_regular_user_cannot_execute_query(self):
        # Arrange
        self.client.force_authenticate(user=self.fixture.user)

        # Act
        response = self.client.post(
            self.url, {"query": "SELECT * FROM structure_customer"}
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_query_parameter_is_required(self):
        # Arrange
        self.client.force_authenticate(user=self.fixture.staff)

        # Act
        response = self.client.post(self.url, {})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"error": "Query parameter is required"})

    def test_anonymous_user_cannot_execute_query(self):
        # Act
        response = self.client.post(
            self.url, {"query": "SELECT * FROM structure_customer"}
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
