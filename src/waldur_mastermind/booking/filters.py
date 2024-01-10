from collections.abc import Iterable

from django.core import exceptions as django_exceptions
from django.db.models import Q
from django_filters import OrderingFilter, UUIDFilter
from rest_framework.filters import BaseFilterBackend

from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.managers import get_connected_customers
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.filters import ResourceFilter

from . import PLUGIN_NAME


class ResourceOwnerOrCreatorFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user = request.user
        if user.is_staff:
            return queryset
        else:
            customers = get_connected_customers(
                user, (RoleEnum.CUSTOMER_OWNER, RoleEnum.CUSTOMER_MANAGER)
            )
            try:
                resource_ids = marketplace_models.Order.objects.filter(
                    type=marketplace_models.RequestTypeMixin.Types.CREATE,
                    offering__type=PLUGIN_NAME,
                    created_by=user,
                ).values_list("resource_id", flat=True)
            except (
                django_exceptions.ObjectDoesNotExist,
                django_exceptions.MultipleObjectsReturned,
            ):
                resource_ids = []

            return queryset.filter(
                Q(offering__customer_id__in=customers) | Q(id__in=resource_ids)
            )


class CustomersFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user = request.user
        if user.is_staff:
            return queryset
        else:
            customers = get_connected_customers(user, RoleEnum.CUSTOMER_OWNER)
            return queryset.filter(customer_id__in=customers)


class SchedulesOrderingFilter(OrderingFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.extra["choices"] += [
            ("schedules", "Schedules"),
            ("-schedules", "Schedules (descending)"),
        ]

    def filter(self, qs, value):
        if isinstance(value, Iterable) and any(
            v in ["schedules", "-schedules"] for v in value
        ):
            # This code works if the first record is the earliest booking.
            # TODO: Add model 'Slot'
            qs = qs.extra(
                select={
                    "schedules": "((marketplace_resource.attributes::json->'schedules'->>0)::json->>'start')"
                }
            )

        return super().filter(qs, value)


class BookingResourceFilter(ResourceFilter):
    o = SchedulesOrderingFilter(fields=("name", "created", "type"))
    connected_customer_uuid = UUIDFilter(method="filter_connected_customer")

    def filter_connected_customer(self, queryset, name, value):
        return queryset.filter(
            Q(
                project__customer__uuid=value,
            )
            | Q(
                offering__customer__uuid=value,
            )
        )
