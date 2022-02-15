import django_filters
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters


class CustomerCreateRequestFilter(django_filters.FilterSet):
    state = core_filters.ReviewStateFilter()


class ProjectCreateRequestFilter(django_filters.FilterSet):
    state = core_filters.ReviewStateFilter()
    customer = core_filters.URLFilter(
        view_name='customer-detail', field_name='flow__customer__uuid'
    )
    customer_uuid = django_filters.UUIDFilter(field_name='flow__customer__uuid')
    new_customer = django_filters.BooleanFilter(
        method='filter_new_customer', widget=BooleanWidget
    )

    def filter_new_customer(self, queryset, name, value):
        if value:
            queryset = queryset.filter(customer__isnull=True)
        return queryset


class ResourceCreateRequestFilter(django_filters.FilterSet):
    state = core_filters.ReviewStateFilter()
    offering = core_filters.URLFilter(
        view_name='marketplace-offering-detail',
        field_name='offering__uuid',
    )
    offering_uuid = django_filters.UUIDFilter(field_name='offering__uuid')
    service_provider = core_filters.URLFilter(
        view_name='customer-detail',
        field_name='offering__customer__uuid',
    )
    service_provider_uuid = django_filters.UUIDFilter(
        field_name='offering__customer__uuid'
    )


class FlowFilter(django_filters.FilterSet):
    state = core_filters.ReviewStateFilter()
    user = core_filters.URLFilter(
        field_name='requested_by__uuid', view_name='user-detail'
    )
    user_uuid = django_filters.UUIDFilter(field_name='requested_by__uuid')


class OfferingActivateRequestFilter(django_filters.FilterSet):
    state = core_filters.ReviewStateFilter()
    offering_uuid = django_filters.UUIDFilter(field_name='offering__uuid')
