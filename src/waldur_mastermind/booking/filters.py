import collections

from django_filters import OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.filters import ResourceFilter


class OfferingCustomersFilterBackend(DjangoFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user = request.user
        if user.is_staff:
            return queryset
        else:
            customers = structure_models.CustomerPermission.objects.filter(
                user=user, role=structure_models.CustomerRole.OWNER
            ).values_list('customer', flat=True)
            return queryset.filter(offering__customer_id__in=customers)


class CustomersFilterBackend(DjangoFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user = request.user
        if user.is_staff:
            return queryset
        else:
            customers = structure_models.CustomerPermission.objects.filter(
                user=user, role=structure_models.CustomerRole.OWNER
            ).values_list('customer', flat=True)
            return queryset.filter(customer_id__in=customers)


class SchedulesOrderingFilter(OrderingFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.extra['choices'] += [
            ('schedules', 'Schedules'),
            ('-schedules', 'Schedules (descending)'),
        ]

    def filter(self, qs, value):
        if isinstance(value, collections.Iterable) and any(
            v in ['schedules', '-schedules'] for v in value
        ):
            # This code works if the first record is the earliest booking.
            # TODO: Add model 'Slot'
            qs = qs.extra(
                select={
                    'schedules': "((marketplace_resource.attributes::json->'schedules'->>0)::json->>'start')"
                }
            )

        return super().filter(qs, value)


class BookingResourceFilter(ResourceFilter):
    o = SchedulesOrderingFilter(fields=('name', 'created', 'type'))
