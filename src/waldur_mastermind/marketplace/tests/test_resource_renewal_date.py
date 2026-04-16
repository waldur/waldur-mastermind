"""
Tests for ResourceSerializer renewal_date field functionality.
"""

import datetime
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, serializers
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import fixtures


class ResourceRenewalDateTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.serializer = serializers.ResourceSerializer()

    def test_monthly_renewal_date_calculation(self):
        """Test renewal date calculation for monthly limit periods."""
        # Create offering with monthly limit component
        component = self.fixture.offering_component
        component.billing_type = BillingTypes.LIMIT
        component.limit_period = LimitPeriods.MONTH
        component.save()

        # Create resource
        resource = self.fixture.resource

        # Mock current date to a specific date for testing
        test_date = datetime.date(2025, 3, 15)  # March 15, 2025
        with patch("django.utils.timezone.now") as mock_now:
            mock_now.return_value.date.return_value = test_date

            renewal_dates = self.serializer.get_renewal_date(resource)

            # Should return dictionary with component type and renewal date
            expected_date = datetime.date(2025, 4, 1)
            self.assertIsInstance(renewal_dates, dict)
            self.assertIn(component.type, renewal_dates)
            self.assertEqual(renewal_dates[component.type], expected_date)

    def test_monthly_renewal_date_on_first_day(self):
        """Test renewal date calculation when current date is first day of month."""
        # Create offering with monthly limit component
        component = self.fixture.offering_component
        component.billing_type = BillingTypes.LIMIT
        component.limit_period = LimitPeriods.MONTH
        component.save()

        resource = self.fixture.resource

        # Test when current date is first day of month
        test_date = datetime.date(2025, 3, 1)  # March 1, 2025
        with patch("django.utils.timezone.now") as mock_now:
            mock_now.return_value.date.return_value = test_date

            renewal_dates = self.serializer.get_renewal_date(resource)

            # Should return April 1, 2025 (next month)
            expected_date = datetime.date(2025, 4, 1)
            self.assertIsInstance(renewal_dates, dict)
            self.assertEqual(renewal_dates[component.type], expected_date)

    def test_quarterly_renewal_date_calculation(self):
        """Test renewal date calculation for quarterly limit periods."""
        # Create offering with quarterly limit component
        component = self.fixture.offering_component
        component.billing_type = BillingTypes.LIMIT
        component.limit_period = LimitPeriods.QUARTERLY
        component.save()

        resource = self.fixture.resource

        test_cases = [
            # (current_date, expected_renewal_date)
            (datetime.date(2025, 2, 15), datetime.date(2025, 4, 1)),  # Q1 -> Q2
            (datetime.date(2025, 5, 10), datetime.date(2025, 7, 1)),  # Q2 -> Q3
            (datetime.date(2025, 8, 20), datetime.date(2025, 10, 1)),  # Q3 -> Q4
            (
                datetime.date(2025, 11, 5),
                datetime.date(2026, 1, 1),
            ),  # Q4 -> Q1 next year
        ]

        for test_date, expected_date in test_cases:
            with patch("django.utils.timezone.now") as mock_now:
                mock_now.return_value.date.return_value = test_date

                renewal_dates = self.serializer.get_renewal_date(resource)

                self.assertIsInstance(renewal_dates, dict)
                self.assertIn(component.type, renewal_dates)
                self.assertEqual(
                    renewal_dates[component.type],
                    expected_date,
                    f"Failed for test_date {test_date}, got {renewal_dates[component.type]}, expected {expected_date}",
                )

    def test_annual_renewal_date_calculation(self):
        """Test renewal date calculation for annual limit periods."""
        # Create offering with annual limit component
        component = self.fixture.offering_component
        component.billing_type = BillingTypes.LIMIT
        component.limit_period = LimitPeriods.ANNUAL
        component.save()

        resource = self.fixture.resource

        test_cases = [
            # (current_date, expected_renewal_date)
            (datetime.date(2025, 3, 15), datetime.date(2026, 1, 1)),
            (datetime.date(2025, 12, 31), datetime.date(2026, 1, 1)),
            (datetime.date(2024, 6, 10), datetime.date(2025, 1, 1)),
        ]

        for test_date, expected_date in test_cases:
            with patch("django.utils.timezone.now") as mock_now:
                mock_now.return_value.date.return_value = test_date

                renewal_dates = self.serializer.get_renewal_date(resource)

                self.assertIsInstance(renewal_dates, dict)
                self.assertIn(component.type, renewal_dates)
                self.assertEqual(
                    renewal_dates[component.type],
                    expected_date,
                    f"Failed for test_date {test_date}, got {renewal_dates[component.type]}, expected {expected_date}",
                )

    def test_total_limit_period_returns_empty_dict(self):
        """Test that total limit periods return empty dict (no renewal)."""
        # Create offering with total limit component
        component = self.fixture.offering_component
        component.billing_type = BillingTypes.LIMIT
        component.limit_period = LimitPeriods.TOTAL
        component.save()

        resource = self.fixture.resource

        renewal_dates = self.serializer.get_renewal_date(resource)
        # Should return empty dict since TOTAL components don't have renewals
        self.assertEqual(renewal_dates, {})

    def test_no_offering_returns_none(self):
        """Test that resources without offering return None."""

        # Create a resource without setting up offering components
        # We'll test by directly calling the method with a mock resource
        class MockResource:
            offering = None
            offering_id = None

        resource = MockResource()
        renewal_date = self.serializer.get_renewal_date(resource)
        self.assertIsNone(renewal_date)

    def test_no_limit_components_returns_none(self):
        """Test that resources without limit-based components return None."""
        # Create offering with non-limit components
        component = self.fixture.offering_component
        component.billing_type = BillingTypes.USAGE
        component.save()

        resource = self.fixture.resource

        renewal_date = self.serializer.get_renewal_date(resource)
        self.assertIsNone(renewal_date)

    def test_mixed_billing_types_uses_limit_components(self):
        """Test that only limit-based components are considered."""
        offering = self.fixture.offering

        # Create multiple components with different billing types
        models.OfferingComponent.objects.filter(offering=offering).delete()

        # Non-limit component
        models.OfferingComponent.objects.create(
            offering=offering,
            type="usage_component",
            name="Usage Component",
            billing_type=BillingTypes.USAGE,
        )

        # Limit component
        models.OfferingComponent.objects.create(
            offering=offering,
            type="limit_component",
            name="Limit Component",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )

        resource = self.fixture.resource

        test_date = datetime.date(2025, 3, 15)
        with patch("django.utils.timezone.now") as mock_now:
            mock_now.return_value.date.return_value = test_date

            renewal_dates = self.serializer.get_renewal_date(resource)

            # Should return renewal date only for limit component
            expected_date = datetime.date(2025, 4, 1)
            self.assertIsInstance(renewal_dates, dict)
            self.assertEqual(len(renewal_dates), 1)  # Only one limit component
            self.assertIn("limit_component", renewal_dates)
            self.assertNotIn("usage_component", renewal_dates)
            self.assertEqual(renewal_dates["limit_component"], expected_date)

    def test_multiple_limit_periods_returns_all(self):
        """Test that all limit-based components are included in the result."""
        offering = self.fixture.offering

        # Create multiple limit components with different periods
        models.OfferingComponent.objects.filter(offering=offering).delete()

        models.OfferingComponent.objects.create(
            offering=offering,
            type="monthly_limit",
            name="Monthly Limit",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )

        models.OfferingComponent.objects.create(
            offering=offering,
            type="annual_limit",
            name="Annual Limit",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.ANNUAL,
        )

        resource = self.fixture.resource

        test_date = datetime.date(2025, 3, 15)
        with patch("django.utils.timezone.now") as mock_now:
            mock_now.return_value.date.return_value = test_date

            renewal_dates = self.serializer.get_renewal_date(resource)

            # Should return renewal dates for all limit components
            self.assertIsInstance(renewal_dates, dict)
            self.assertEqual(len(renewal_dates), 2)

            # Check monthly component
            self.assertIn("monthly_limit", renewal_dates)
            self.assertEqual(renewal_dates["monthly_limit"], datetime.date(2025, 4, 1))

            # Check annual component
            self.assertIn("annual_limit", renewal_dates)
            self.assertEqual(renewal_dates["annual_limit"], datetime.date(2026, 1, 1))

    def test_all_total_components_returns_empty_dict(self):
        """Test that offerings with only TOTAL limit components return empty dict."""
        offering = self.fixture.offering

        # Create only TOTAL limit components
        models.OfferingComponent.objects.filter(offering=offering).delete()

        models.OfferingComponent.objects.create(
            offering=offering,
            type="total_bandwidth",
            name="Total Bandwidth",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )

        resource = self.fixture.resource

        renewal_dates = self.serializer.get_renewal_date(resource)

        # Should return empty dict (not None) since there are limit components
        # but none have renewable periods
        self.assertEqual(renewal_dates, {})


class ResourceSerializerRenewalDateIntegrationTest(TestCase):
    """Integration tests for renewal_date field in API responses."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.marketplace_fixture = fixtures.MarketplaceFixture()
        self.client = APIClient()

    def test_renewal_date_in_api_response(self):
        """Test that renewal_date field appears in resource API responses."""
        # Create offering with monthly limit component
        component = self.marketplace_fixture.offering_component
        component.billing_type = BillingTypes.LIMIT
        component.limit_period = LimitPeriods.MONTH
        component.save()

        resource = self.marketplace_fixture.resource

        # Authenticate as staff user
        self.client.force_authenticate(self.fixture.staff)

        # Get resource via API
        url = f"/api/marketplace-resources/{resource.uuid}/"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("renewal_date", response.data)

        # Verify renewal_date is a dict for limit-based components
        self.assertIsInstance(response.data["renewal_date"], dict)
        self.assertIn(component.type, response.data["renewal_date"])

    def test_renewal_date_null_for_non_limit_components(self):
        """Test that renewal_date is null for resources without limit components."""
        # Create offering with usage component (not limit)
        component = self.marketplace_fixture.offering_component
        component.billing_type = BillingTypes.USAGE
        component.save()

        resource = self.marketplace_fixture.resource

        # Authenticate as staff user
        self.client.force_authenticate(self.fixture.staff)

        # Get resource via API
        url = f"/api/marketplace-resources/{resource.uuid}/"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("renewal_date", response.data)

        # Verify renewal_date is None for non-limit components
        self.assertIsNone(response.data["renewal_date"])
