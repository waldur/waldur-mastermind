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
