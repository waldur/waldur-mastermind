from unittest.mock import Mock

from django.test import TestCase

from waldur_core.structure import serializers as structure_serializers
from waldur_mastermind.billing import serializers as billing_serializers
from waldur_mastermind.invoices import serializers as invoice_serializers


class SerializerOptimizationTest(TestCase):
    """Test that eager_load optimizations don't cause infinite recursion."""

    def test_multiple_optimizations_dont_cause_recursion(self):
        """Test that applying optimizations from both billing and invoices modules doesn't cause recursion."""
        # Create a mock CustomerSerializer with proper attributes
        mock_serializer = Mock(spec=structure_serializers.CustomerSerializer)
        mock_serializer.__name__ = "CustomerSerializer"

        # Create a simple function to act as eager_load
        def original_eager_load(queryset, request=None):
            return queryset

        mock_serializer.eager_load = original_eager_load

        # Apply billing optimization
        billing_serializers._optimize_customer_serializer_eager_load(mock_serializer)
        billing_optimized_method = mock_serializer.eager_load

        # Should have replaced the method
        self.assertIsNot(original_eager_load, billing_optimized_method)
        self.assertTrue(hasattr(billing_optimized_method, "_billing_optimized"))

        # Apply invoice optimization for credit
        invoice_serializers._optimize_customer_serializer_eager_load_for_credit(
            mock_serializer
        )
        credit_optimized_method = mock_serializer.eager_load

        # Should have replaced the method again
        self.assertIsNot(billing_optimized_method, credit_optimized_method)
        self.assertTrue(hasattr(credit_optimized_method, "_credit_optimized"))

        # Re-applying invoice optimization should do nothing (protection working)
        invoice_serializers._optimize_customer_serializer_eager_load_for_credit(
            mock_serializer
        )
        self.assertIs(credit_optimized_method, mock_serializer.eager_load)

        # Test that calling the optimized method doesn't cause recursion
        mock_request = Mock()
        mock_request.query_params.getlist = Mock(return_value=[])
        mock_queryset = Mock()
        mock_queryset.select_related = Mock(return_value=mock_queryset)

        # This should not raise RecursionError
        result = mock_serializer.eager_load(mock_queryset, mock_request)
        self.assertIsNotNone(result)

    def test_billing_optimization_flag_prevents_double_application(self):
        """Test that _billing_optimized flag prevents double application."""
        mock_serializer = Mock(spec=structure_serializers.CustomerSerializer)
        mock_serializer.__name__ = "CustomerSerializer"
        mock_serializer.eager_load = Mock(return_value="queryset")

        # Apply optimization once
        billing_serializers._optimize_customer_serializer_eager_load(mock_serializer)
        first_method = mock_serializer.eager_load

        # Check flag is set
        self.assertTrue(hasattr(first_method, "_billing_optimized"))

        # Apply again - should return early
        billing_serializers._optimize_customer_serializer_eager_load(mock_serializer)
        second_method = mock_serializer.eager_load

        # Method should be the same object
        self.assertIs(first_method, second_method)

    def test_credit_optimization_flag_prevents_double_application(self):
        """Test that _credit_optimized flag prevents double application."""
        mock_serializer = Mock(spec=structure_serializers.CustomerSerializer)
        mock_serializer.__name__ = "CustomerSerializer"
        mock_serializer.eager_load = Mock(return_value="queryset")

        # Apply optimization once
        invoice_serializers._optimize_customer_serializer_eager_load_for_credit(
            mock_serializer
        )
        first_method = mock_serializer.eager_load

        # Check flag is set
        self.assertTrue(hasattr(first_method, "_credit_optimized"))

        # Apply again - should return early
        invoice_serializers._optimize_customer_serializer_eager_load_for_credit(
            mock_serializer
        )
        second_method = mock_serializer.eager_load

        # Method should be the same object
        self.assertIs(first_method, second_method)

    def test_eager_load_still_works_after_optimizations(self):
        """Test that eager_load method still works after applying optimizations."""
        mock_serializer = Mock(spec=structure_serializers.CustomerSerializer)
        mock_serializer.__name__ = "CustomerSerializer"

        # Create a mock eager_load that returns a queryset
        mock_queryset = Mock()
        mock_queryset.select_related = Mock(return_value=mock_queryset)
        original_eager_load = Mock(return_value=mock_queryset)
        mock_serializer.eager_load = original_eager_load

        # Apply both optimizations
        billing_serializers._optimize_customer_serializer_eager_load(mock_serializer)
        invoice_serializers._optimize_customer_serializer_eager_load_for_credit(
            mock_serializer
        )

        # Create a mock request
        mock_request = Mock()
        mock_request.query_params.getlist = Mock(return_value=[])

        # Call the optimized method
        result = mock_serializer.eager_load(mock_queryset, mock_request)

        # Should return a queryset
        self.assertIsNotNone(result)

        # Original should have been called exactly once (no recursion)
        original_eager_load.assert_called_once()

    def test_recursion_fix_with_circular_optimizations(self):
        """Test that applying optimizations in any order doesn't cause recursion."""
        # Test case 1: billing first, then credit
        mock_serializer1 = Mock(spec=structure_serializers.CustomerSerializer)
        mock_serializer1.__name__ = "CustomerSerializer"

        original_eager_load1 = Mock(return_value=Mock())
        mock_serializer1.eager_load = original_eager_load1

        # Apply billing first
        billing_serializers._optimize_customer_serializer_eager_load(mock_serializer1)
        # Apply credit second
        invoice_serializers._optimize_customer_serializer_eager_load_for_credit(
            mock_serializer1
        )

        # Test case 2: credit first, then billing
        mock_serializer2 = Mock(spec=structure_serializers.CustomerSerializer)
        mock_serializer2.__name__ = "CustomerSerializer"

        original_eager_load2 = Mock(return_value=Mock())
        mock_serializer2.eager_load = original_eager_load2

        # Apply credit first
        invoice_serializers._optimize_customer_serializer_eager_load_for_credit(
            mock_serializer2
        )
        # Apply billing second
        billing_serializers._optimize_customer_serializer_eager_load(mock_serializer2)

        # Both final methods should have both optimization flags
        self.assertTrue(hasattr(mock_serializer1.eager_load, "_billing_optimized"))
        self.assertTrue(hasattr(mock_serializer1.eager_load, "_credit_optimized"))
        self.assertTrue(hasattr(mock_serializer2.eager_load, "_billing_optimized"))
        self.assertTrue(hasattr(mock_serializer2.eager_load, "_credit_optimized"))

        # Test that calling either method works without recursion
        mock_request = Mock()
        mock_request.query_params.getlist = Mock(
            return_value=["billing_price_estimate", "customer_credit"]
        )
        mock_queryset = Mock()
        mock_queryset.select_related = Mock(return_value=mock_queryset)

        # These should not raise RecursionError
        result1 = mock_serializer1.eager_load(mock_queryset, mock_request)
        result2 = mock_serializer2.eager_load(mock_queryset, mock_request)

        self.assertIsNotNone(result1)
        self.assertIsNotNone(result2)

        # Original methods should have been called exactly once each (no recursion)
        original_eager_load1.assert_called_once()
        original_eager_load2.assert_called_once()

    def test_combined_optimization_handles_all_fields(self):
        """Test that combined optimization handles both billing and credit fields correctly."""
        mock_serializer = Mock(spec=structure_serializers.CustomerSerializer)
        mock_serializer.__name__ = "CustomerSerializer"

        # Create a queryset mock that returns itself from select_related
        mock_queryset = Mock()
        mock_queryset.select_related = Mock(return_value=mock_queryset)

        original_eager_load = Mock(return_value=mock_queryset)
        mock_serializer.eager_load = original_eager_load

        # Apply both optimizations
        billing_serializers._optimize_customer_serializer_eager_load(mock_serializer)
        invoice_serializers._optimize_customer_serializer_eager_load_for_credit(
            mock_serializer
        )

        # Verify both optimization flags are present on the final method
        self.assertTrue(hasattr(mock_serializer.eager_load, "_billing_optimized"))
        self.assertTrue(hasattr(mock_serializer.eager_load, "_credit_optimized"))

        # Test that the combined method works without errors
        mock_request = Mock()
        mock_request.query_params.getlist = Mock(
            return_value=["billing_price_estimate", "customer_credit"]
        )

        # This call should not raise any exception (recursion would cause RecursionError)
        result = mock_serializer.eager_load(mock_queryset, mock_request)

        # Verify the result is a queryset (our mock)
        self.assertIsNotNone(result)

        # Verify original method was called (proving no infinite recursion)
        original_eager_load.assert_called_once()
