import django_filters
from django.db.models.query_utils import Q
from django_filters.widgets import BooleanWidget

from waldur_core.core.filters import (
    CreatedModifiedFilter,
    ExtendedOrderingFilter,
    URLFilter,
    filter_by_full_name,
)

from . import models


class RoleFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter(widget=BooleanWidget)
    name = django_filters.CharFilter(lookup_expr="icontains")
    description = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        model = models.Role
        fields = ["is_active", "name", "description"]


class UserPermissionFilter(CreatedModifiedFilter, django_filters.FilterSet):
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
    scope_type = django_filters.CharFilter(
        method="filter_scope_type", label="Scope type"
    )
    scope_uuid = django_filters.UUIDFilter(
        method="filter_scope_uuid", label="Scope UUID"
    )
    scope_name = django_filters.CharFilter(
        method="filter_scope_name", label="Scope name"
    )
    role_name = django_filters.CharFilter(
        field_name="role__name",
        lookup_expr="icontains",
        label="Role name contains",
    )
    role_uuid = django_filters.UUIDFilter(
        field_name="role__uuid",
        lookup_expr="exact",
        label="Role UUID",
    )

    def filter_by_full_name(self, queryset, name, value):
        return filter_by_full_name(queryset, value, "user")

    def filter_scope_type(self, queryset, name, value):
        return queryset.filter(content_type__model=value)

    def _get_scope_objects_query(self, field_name, value, filter_condition):
        """
        Helper method to build a query across all scope models.

        Args:
            field_name: Field name to filter on (e.g., 'uuid', 'name')
            value: Value to filter by
            filter_condition: A function that takes (model, value) and returns a filter dict

        Returns:
            A Q object for filtering the queryset
        """
        content_types = self.Meta.model.get_scope_content_types()
        ct_to_model = {ct: model for model, ct in content_types.items()}
        query = Q()

        for ct, model in ct_to_model.items():
            if not hasattr(model, field_name):
                continue
            try:
                filter_kwargs = filter_condition(model, value)
                matching_objects = model.objects.filter(**filter_kwargs)

                if matching_objects.exists():
                    object_ids = matching_objects.values_list("id", flat=True)
                    query |= Q(content_type=ct, object_id__in=object_ids)
            except Exception:
                pass

        return query

    def filter_scope_uuid(self, queryset, name, value):
        query = self._get_scope_objects_query(
            field_name="uuid",
            value=value,
            filter_condition=lambda model, val: {"uuid": val},
        )
        return queryset.filter(query) if query else queryset.none()

    def filter_scope_name(self, queryset, name, value):
        query = self._get_scope_objects_query(
            field_name="name",
            value=value,
            filter_condition=lambda model, val: {"name__icontains": val},
        )
        return queryset.filter(query) if query else queryset.none()

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
