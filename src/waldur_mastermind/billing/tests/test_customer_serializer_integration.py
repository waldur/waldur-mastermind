"""
Integration tests for CustomerSerializer eager_load optimizations.

These tests ensure that the optimizations applied to CustomerSerializer
don't cause recursion errors when accessing the /api/customers/ endpoint.
"""

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from waldur_core.structure.tests import fixtures


class CustomerSerializerIntegrationTest(TestCase):
    """Test CustomerSerializer optimizations in real API scenarios."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.client = APIClient()

    def test_customers_list_endpoint_with_billing_and_credit_fields(self):
        """Test that /api/customers/ endpoint works with both billing and credit fields."""
        # Authenticate as a user who can list customers
        self.client.force_authenticate(user=self.fixture.owner)

        # Test the endpoint that triggered the original recursion error
        url = "/api/customers/"
        params = {"field": ["billing_price_estimate", "customer_credit"]}

        # This should not cause RecursionError
        response = self.client.get(url, params)

        # We expect either 200 (success) or 403 (forbidden) but not 500 (server error)
        # The important thing is that we don't get a RecursionError
        self.assertIn(
            response.status_code, [status.HTTP_200_OK, status.HTTP_403_FORBIDDEN]
        )

        # If we got a 500 error, the recursion issue would likely be in the response
        self.assertNotEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_customers_list_endpoint_with_only_billing_field(self):
        """Test that /api/customers/ endpoint works with only billing field."""
        self.client.force_authenticate(user=self.fixture.owner)

        url = "/api/customers/"
        params = {"field": ["billing_price_estimate"]}

        response = self.client.get(url, params)

        # Should not cause server error
        self.assertNotEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_customers_list_endpoint_with_only_credit_field(self):
        """Test that /api/customers/ endpoint works with only credit field."""
        self.client.force_authenticate(user=self.fixture.owner)

        url = "/api/customers/"
        params = {"field": ["customer_credit"]}

        response = self.client.get(url, params)

        # Should not cause server error
        self.assertNotEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_customers_list_endpoint_without_optimization_fields(self):
        """Test that /api/customers/ endpoint works without optimization fields."""
        self.client.force_authenticate(user=self.fixture.owner)

        url = "/api/customers/"

        response = self.client.get(url)

        # Should not cause server error
        self.assertNotEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_customers_retrieve_endpoint_with_billing_and_credit_fields(self):
        """Test that individual customer retrieval works with optimization fields."""
        self.client.force_authenticate(user=self.fixture.owner)

        url = f"/api/customers/{self.fixture.customer.uuid}/"
        params = {"field": ["billing_price_estimate", "customer_credit"]}

        # This should not cause RecursionError
        response = self.client.get(url, params)

        # Should not cause server error
        self.assertNotEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_recursion_fix_survives_multiple_requests(self):
        """Test that the fix works consistently across multiple requests."""
        self.client.force_authenticate(user=self.fixture.owner)

        url = "/api/customers/"
        params = {"field": ["billing_price_estimate", "customer_credit"]}

        # Make multiple requests to ensure the optimizations stay stable
        for i in range(3):
            with self.subTest(request_number=i + 1):
                response = self.client.get(url, params)
                self.assertNotEqual(
                    response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR
                )

    def test_optimization_works_with_staff_user(self):
        """Test that optimizations work for staff users."""
        self.client.force_authenticate(user=self.fixture.staff)

        url = "/api/customers/"
        params = {"field": ["billing_price_estimate", "customer_credit"]}

        response = self.client.get(url, params)

        # Should not cause server error regardless of user type
        self.assertNotEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
