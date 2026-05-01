import django_filters
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters
from waldur_core.core.filters import (
    CreatedModifiedFilter,
    ExtendedOrderingFilter,
    URLFilter,
    filter_by_full_name,
    get_generic_field_filter,
)
from waldur_core.permissions.mixins import get_permission_aggregates

from . import models


class RoleFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter(widget=BooleanWidget)
    name = django_filters.CharFilter(lookup_expr="icontains")
    description = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        model = models.Role
        fields = ["is_active", "name", "description"]


class RoleAvailabilityFilter(django_filters.FilterSet):
    role_uuid = core_filters.RelatedUUIDFilter(
        view_name="role-detail",
        field_name="role__uuid",
        label="Role UUID",
    )
    role_name = django_filters.CharFilter(
        field_name="role__name",
        lookup_expr="icontains",
        label="Role name contains",
    )
    scope_type = django_filters.CharFilter(
        method="filter_scope_type",
        label="Scope content type (e.g. 'offering', 'customer')",
    )
    object_id = django_filters.NumberFilter(label="Scope object id")

    def filter_scope_type(self, queryset, name, value):
        return queryset.filter(content_type__model=value)

    class Meta:
        model = models.RoleAvailability
        fields = ["role_uuid", "role_name", "scope_type", "object_id"]


class UserPermissionFilter(CreatedModifiedFilter, django_filters.FilterSet):
    user = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="user__uuid"
    )
    user_url = URLFilter(
        view_name="user-detail",
        field_name="user__uuid",
    )
    username = django_filters.CharFilter(
        field_name="user__username",
        lookup_expr="exact",
    )
    full_name = django_filters.CharFilter(
        method="filter_by_full_name", label="User full name contains"
    )
    native_name = django_filters.CharFilter(
        field_name="user__native_name",
        lookup_expr="icontains",
    )
    user_slug = django_filters.CharFilter(
        field_name="user__slug",
        lookup_expr="icontains",
        label="User slug contains",
    )
    scope_type = django_filters.CharFilter(
        method="filter_scope_type", label="Scope type"
    )
    scope_uuid = django_filters.UUIDFilter(
        method=get_generic_field_filter(models_to_search=get_permission_aggregates()),
        label="Scope UUID",
    )
    scope_name = django_filters.CharFilter(
        method=get_generic_field_filter(
            models_to_search=get_permission_aggregates(),
            field_name="name",
            lookup_expr="icontains",
        ),
        label="Scope name",
    )
    role_name = django_filters.CharFilter(
        field_name="role__name",
        lookup_expr="icontains",
        label="Role name contains",
    )
    role_uuid = core_filters.RelatedUUIDFilter(
        view_name="role-detail",
        field_name="role__uuid",
        lookup_expr="exact",
        label="Role UUID",
    )

    def filter_by_full_name(self, queryset, name, value):
        return filter_by_full_name(queryset, value, "user")

    def filter_scope_type(self, queryset, name, value):
        return queryset.filter(content_type__model=value)

    o = ExtendedOrderingFilter(
        fields=(
            ("user__username", "username"),
            (("user__first_name", "user__last_name"), "full_name"),
            ("user__native_name", "native_name"),
            ("user__email", "email"),
            ("expiration_time", "expiration_time"),
            ("created", "created"),
            ("role", "role"),
        )
    )

    class Meta:
        model = models.UserRole
        fields = [
            "created",
            "modified",
            "expiration_time",
            "user",
            "user_url",
            "username",
            "full_name",
            "native_name",
            "user_slug",
            "scope_type",
            "scope_uuid",
            "scope_name",
            "role_name",
            "role_uuid",
            "o",
        ]
