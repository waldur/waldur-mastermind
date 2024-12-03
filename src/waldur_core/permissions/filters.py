import django_filters
from django_filters.widgets import BooleanWidget

from waldur_core.core.filters import (
    ExtendedOrderingFilter,
    URLFilter,
    filter_by_full_name,
)

from . import models


class RoleFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter(widget=BooleanWidget)

    class Meta:
        model = models.Role
        fields = ["is_active"]


class UserPermissionFilter(django_filters.FilterSet):
    user = django_filters.UUIDFilter(field_name="user__uuid")
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

    def filter_by_full_name(self, queryset, name, value):
        return filter_by_full_name(queryset, value, "user")

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
