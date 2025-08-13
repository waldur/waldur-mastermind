from decimal import Decimal

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.billing.tests.utils import get_financial_report_url
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.invoices.tests import fixtures as invoice_fixtures


@ddt
class BillingOptimizationCorrectnessTest(test.APITransactionTestCase):
    """
    Test that the billing optimization returns identical results to the standard logic.
    """

    def setUp(self):
        self.fixture = invoice_fixtures.InvoiceFixture()

        # Create invoice items with known values for testing
        self.item1 = invoice_factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=self.fixture.project,
            unit=invoice_models.InvoiceItem.Units.QUANTITY,
            unit_price=Decimal("15.50"),
            quantity=4,  # total = 62.00
        )
        self.item2 = invoice_factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=self.fixture.project,
            unit=invoice_models.InvoiceItem.Units.QUANTITY,
            unit_price=Decimal("25.75"),
            quantity=2,  # total = 51.50
        )
        # Expected customer total = 113.50

    def test_billing_price_estimate_optimization_correctness(self):
        """Test that optimized and standard billing_price_estimate return identical values."""

        self.client.force_authenticate(self.fixture.staff)

        # Get result without optimization (no billing_price_estimate field requested)
        response_standard = self.client.get(
            get_financial_report_url(self.fixture.customer)
        )
        self.assertEqual(response_standard.status_code, status.HTTP_200_OK)
        standard_result = response_standard.data["billing_price_estimate"]

        # Get result with optimization (billing_price_estimate field requested explicitly)
        response_optimized = self.client.get(
            get_financial_report_url(self.fixture.customer),
            {"field": "billing_price_estimate"},  # This should trigger optimization
        )
        self.assertEqual(response_optimized.status_code, status.HTTP_200_OK)
        optimized_result = response_optimized.data["billing_price_estimate"]

        # Verify they are identical
        self.assertEqual(standard_result["total"], optimized_result["total"])
        self.assertEqual(standard_result["current"], optimized_result["current"])
        self.assertEqual(standard_result["tax"], optimized_result["tax"])
        self.assertEqual(
            standard_result["tax_current"], optimized_result["tax_current"]
        )

        # Verify the calculation is correct (62.00 + 51.50 = 113.50)
        self.assertEqual(float(standard_result["total"]), 113.50)
        self.assertEqual(float(optimized_result["total"]), 113.50)

    def test_billing_price_estimate_optimization_with_multiple_customers(self):
        """Test optimization correctness with customer list API (bulk optimization)."""

        # Create additional customers with different invoice amounts
        fixture2 = invoice_fixtures.InvoiceFixture()
        invoice_factories.InvoiceItemFactory(
            invoice=fixture2.invoice,
            project=fixture2.project,
            unit_price=Decimal("10.00"),
            quantity=3,  # total = 30.00
        )

        fixture3 = invoice_fixtures.InvoiceFixture()
        invoice_factories.InvoiceItemFactory(
            invoice=fixture3.invoice,
            project=fixture3.project,
            unit_price=Decimal("7.25"),
            quantity=8,  # total = 58.00
        )

        self.client.force_authenticate(self.fixture.staff)

        # Get individual results for each customer (non-optimized)
        individual_results = {}
        for fixture in [self.fixture, fixture2, fixture3]:
            response = self.client.get(get_financial_report_url(fixture.customer))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            individual_results[fixture.customer.uuid.hex] = response.data[
                "billing_price_estimate"
            ]

        # Get bulk results via customer list API with billing_price_estimate field (optimized)
        response = self.client.get(
            "/api/customers/", {"field": ["uuid", "name", "billing_price_estimate"]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Create a mapping of bulk results by customer UUID
        bulk_results = {}
        for customer_data in response.data:
            bulk_results[customer_data["uuid"]] = customer_data[
                "billing_price_estimate"
            ]

        # Verify each customer's results are identical between individual and bulk APIs
        expected_totals = {
            self.fixture.customer.uuid.hex: 113.50,  # 62.00 + 51.50
            fixture2.customer.uuid.hex: 30.00,  # 10.00 * 3
            fixture3.customer.uuid.hex: 58.00,  # 7.25 * 8
        }

        for customer_uuid, expected_total in expected_totals.items():
            individual = individual_results[customer_uuid]
            bulk = bulk_results[customer_uuid]

            self.assertEqual(
                individual["total"],
                bulk["total"],
                f"Total mismatch for customer {customer_uuid}: individual={individual['total']}, bulk={bulk['total']}",
            )
            self.assertEqual(
                individual["current"],
                bulk["current"],
                f"Current mismatch for customer {customer_uuid}: individual={individual['current']}, bulk={bulk['current']}",
            )
            self.assertEqual(
                individual["tax"],
                bulk["tax"],
                f"Tax mismatch for customer {customer_uuid}: individual={individual['tax']}, bulk={bulk['tax']}",
            )
            self.assertEqual(
                individual["tax_current"],
                bulk["tax_current"],
                f"Tax current mismatch for customer {customer_uuid}: individual={individual['tax_current']}, bulk={bulk['tax_current']}",
            )

            # Verify calculation correctness
            self.assertEqual(
                float(bulk["total"]),
                expected_total,
                f"Incorrect calculation for customer {customer_uuid}: expected={expected_total}, got={bulk['total']}",
            )

    def test_billing_price_estimate_optimization_with_no_estimate(self):
        """Test optimization correctness for customers with no price estimate."""

        # Create a customer with no invoice items
        customer_no_invoice = structure_factories.CustomerFactory()

        self.client.force_authenticate(self.fixture.staff)

        # Get result without optimization
        response_standard = self.client.get(
            get_financial_report_url(customer_no_invoice)
        )
        self.assertEqual(response_standard.status_code, status.HTTP_200_OK)
        standard_result = response_standard.data["billing_price_estimate"]

        # Get result with optimization (via customer list API)
        response = self.client.get(
            "/api/customers/", {"field": ["uuid", "billing_price_estimate"]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Find the customer with no invoice in the results
        optimized_result = None
        for customer_data in response.data:
            if customer_data["uuid"] == customer_no_invoice.uuid.hex:
                optimized_result = customer_data["billing_price_estimate"]
                break

        self.assertIsNotNone(
            optimized_result, "Customer not found in optimized results"
        )

        # Verify both return identical zero values
        self.assertEqual(standard_result["total"], optimized_result["total"])
        self.assertEqual(standard_result["current"], optimized_result["current"])
        self.assertEqual(standard_result["tax"], optimized_result["tax"])
        self.assertEqual(
            standard_result["tax_current"], optimized_result["tax_current"]
        )

        # Verify values are actually zero
        self.assertEqual(float(standard_result["total"]), 0.0)
        self.assertEqual(float(optimized_result["total"]), 0.0)

    @data("year", "month", "both", "neither")
    def test_billing_price_estimate_optimization_with_periods(self, period_type):
        """Test optimization correctness with different period parameters."""

        # Prepare request parameters based on test case
        params = {}
        if period_type in ("year", "both"):
            params["year"] = 2017
        if period_type in ("month", "both"):
            params["month"] = 1

        self.client.force_authenticate(self.fixture.staff)

        # Get result without optimization
        response_standard = self.client.get(
            get_financial_report_url(self.fixture.customer), params
        )
        self.assertEqual(response_standard.status_code, status.HTTP_200_OK)
        standard_result = response_standard.data["billing_price_estimate"]

        # Get result with optimization (same parameters but with field specification)
        params_optimized = params.copy()
        params_optimized["field"] = "billing_price_estimate"
        response_optimized = self.client.get(
            get_financial_report_url(self.fixture.customer), params_optimized
        )
        self.assertEqual(response_optimized.status_code, status.HTTP_200_OK)
        optimized_result = response_optimized.data["billing_price_estimate"]

        # Verify they are identical
        self.assertEqual(standard_result["total"], optimized_result["total"])
        self.assertEqual(standard_result["current"], optimized_result["current"])
        self.assertEqual(standard_result["tax"], optimized_result["tax"])
        self.assertEqual(
            standard_result["tax_current"], optimized_result["tax_current"]
        )
