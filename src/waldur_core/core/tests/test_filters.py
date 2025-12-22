from unittest import mock

from django.db import models
from rest_framework import test

from waldur_core.core import filters as core_filters
from waldur_core.permissions.mixins import get_permission_aggregates
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests.factories import CustomerFactory
from waldur_mastermind.marketplace.models import ServiceProvider


class TestGetGenericFieldFilter(test.APITransactionTestCase):
    """Tests for get_generic_field_filter function."""

    def test_filter_does_not_raise_error_for_models_with_name_property(self):
        """
        Ensure get_generic_field_filter handles models where the target field
        is a property rather than a database field without raising FieldError.

        Models like CallManagingOrganisation and ServiceProvider have 'name'
        accessible via their 'customer' FK and should be searched via customer__name.
        """
        # Create filter function that searches by 'name' field
        filter_func = core_filters.get_generic_field_filter(
            models_to_search=get_permission_aggregates(),
            field_name="name",
            lookup_expr="icontains",
        )

        # This should not raise FieldError
        queryset = UserRole.objects.all()
        result = filter_func(queryset, "scope_name", "test_value")

        # Should return queryset without error (might be empty, that's ok)
        self.assertIsInstance(result, models.QuerySet)

    def test_filter_uses_customer_fallback_for_models_without_direct_name_field(self):
        """
        Verify that models like ServiceProvider and CallManagingOrganisation
        can be searched via their customer__name relationship.
        """
        # Create a customer with a searchable name
        customer = CustomerFactory(name="Test Customer For Filter")

        # Create a ServiceProvider linked to this customer
        ServiceProvider.objects.create(customer=customer)

        # Create filter function
        filter_func = core_filters.get_generic_field_filter(
            models_to_search=[ServiceProvider],
            field_name="name",
            lookup_expr="icontains",
        )

        queryset = UserRole.objects.all()

        # The filter should be able to query via customer__name
        # Without the fix, this would raise FieldError
        result = filter_func(queryset, "scope_name", "Test Customer")
        self.assertIsInstance(result, models.QuerySet)


class TestUrlFilter(test.APITransactionTestCase):
    def setUp(self):
        self.customer = CustomerFactory()
        self.url = CustomerFactory.get_url(self.customer)

        self.customer_filter = core_filters.URLFilter(
            view_name="customer-detail", field_name="customer__uuid"
        )

    def test_filter_checks_that_url_matches_view(self):
        qs = mock.Mock()
        self.customer_filter.filter(qs, self.url)
        qs.filter.assert_called_once_with(customer__uuid__exact=self.customer.uuid.hex)
