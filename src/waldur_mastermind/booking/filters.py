import collections

from django.core import exceptions as django_exceptions
from django.db.models import Q
from django_filters import OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.filters import ResourceFilter

from . import PLUGIN_NAME


class ResourceOwnerOrCreatorFilterBackend(DjangoFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user = request.user
        if user.is_staff:
            return queryset
        else:
            customers = structure_models.CustomerPermission.objects.filter(
                user=user, role=structure_models.CustomerRole.OWNER
            ).values_list('customer', flat=True)

            try:
                resource_ids = marketplace_models.OrderItem.objects.filter(
                    type=marketplace_models.RequestTypeMixin.Types.CREATE,
                    offering__type=PLUGIN_NAME,
                    order__created_by=user,
                ).values_list('resource_id', flat=True)
            except (
                django_exceptions.ObjectDoesNotExist,
                django_exceptions.MultipleObjectsReturned,
            ):
                resource_ids = []

            return queryset.filter(
                Q(offering__customer_id__in=customers) | Q(id__in=resource_ids)
            )


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
