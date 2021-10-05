import django_filters
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters

from . import models


class ReviewStateFilter(core_filters.MappedMultipleChoiceFilter):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault(
            'choices',
            [
                (representation, representation)
                for db_value, representation in models.ReviewMixin.States.CHOICES
            ],
        )
        kwargs.setdefault(
            'choice_mappings',
            {
                representation: db_value
                for db_value, representation in models.ReviewMixin.States.CHOICES
            },
        )
        super().__init__(*args, **kwargs)


class CustomerCreateRequestFilter(django_filters.FilterSet):
    state = ReviewStateFilter()


class ProjectCreateRequestFilter(django_filters.FilterSet):
    state = ReviewStateFilter()
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
    state = ReviewStateFilter()
    offering = core_filters.URLFilter(
        view_name='marketplace-offering-detail', field_name='offering__uuid',
    )
    offering_uuid = django_filters.UUIDFilter(field_name='offering__uuid')
    service_provider = core_filters.URLFilter(
        view_name='customer-detail', field_name='offering__customer__uuid',
    )
    service_provider_uuid = django_filters.UUIDFilter(
        field_name='offering__customer__uuid'
    )


class FlowFilter(django_filters.FilterSet):
    state = ReviewStateFilter()
    user = core_filters.URLFilter(
        field_name='requested_by__uuid', view_name='user-detail'
    )
    user_uuid = django_filters.UUIDFilter(field_name='requested_by__uuid')


class OfferingActivateRequestFilter(django_filters.FilterSet):
    state = ReviewStateFilter()
    offering_uuid = django_filters.UUIDFilter(field_name='offering__uuid')
