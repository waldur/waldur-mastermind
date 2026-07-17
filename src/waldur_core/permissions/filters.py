import django_filters
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters
from waldur_core.core.filters import (
    CreatedModifiedFilter,
    ExtendedOrderingFilter,
    URLFilter,
    filter_by_full_name,
    get_generic_field_filter,
)
from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.permissions.mixins import get_permission_aggregates
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)
from waldur_core.structure.models import Customer, Project

from . import models


class RoleFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter(widget=BooleanWidget)
    is_system_role = django_filters.BooleanFilter(widget=BooleanWidget)
    name = django_filters.CharFilter(lookup_expr="icontains")
    description = django_filters.CharFilter(lookup_expr="icontains")
    available_for_customer = django_filters.UUIDFilter(
        method="filter_available_for_customer",
        label="Roles usable within a customer (uuid): "
        "system roles + org-private, minus concealed",
    )
    include_concealed = django_filters.BooleanFilter(
        method="filter_noop",
        widget=BooleanWidget,
        label="Keep roles concealed for the customer in the "
        "available_for_customer result (staff management view).",
    )
    content_type = django_filters.CharFilter(
        method="filter_content_type",
        label="Comma-separated scope types (e.g. 'customer,project') to keep.",
    )
    query = django_filters.CharFilter(
        method="filter_query",
        label="Search by role name or description.",
    )
    o = ExtendedOrderingFilter(
        fields=(
            # "name" sorts on the display name (description || name); see
            # RoleViewSet.get_queryset which annotates display_name and
            # assigned_users_count.
            ("display_name", "name"),
            ("content_type__model", "scope"),
            ("template__name", "origin"),
            ("assigned_users_count", "users_count"),
            ("is_active", "is_active"),
        )
    )

    def filter_noop(self, queryset, name, value):
        # Applied inside filter_available_for_customer.
        return queryset

    def filter_query(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value) | Q(description__icontains=value)
        )

    def filter_content_type(self, queryset, name, value):
        content_types = []
        for key in (k.strip() for k in value.split(",") if k.strip()):
            if key in TYPE_MAP:
                app_label, model = TYPE_MAP[key]
                content_types.append(
                    ContentType.objects.get_by_natural_key(app_label, model)
                )
        return queryset.filter(content_type__in=content_types)

    def filter_available_for_customer(self, queryset, name, value):
        try:
            customer = Customer.objects.get(uuid=value)
        except Customer.DoesNotExist:
            return queryset.none()
        # A non-member must not probe another organization: the concealment step
        # below is subtractive against roles they can already see, so an
        # unrestricted lookup would let them infer that org's concealed roles.
        user = getattr(self.request, "user", None)
        if not (user and user.is_authenticated):
            return queryset.none()
        if not (user.is_staff or user.is_support):
            connected = set(get_connected_customers(user))
            connected.update(
                Project.objects.filter(id__in=get_connected_projects(user)).values_list(
                    "customer_id", flat=True
                )
            )
            if customer.id not in connected:
                return queryset.none()
        customer_ct = ContentType.objects.get_for_model(Customer)
        available_role_ids = models.RoleAvailability.objects.filter(
            content_type=customer_ct, object_id=customer.id
        ).values_list("role_id", flat=True)
        # Public roles (no availability records) plus roles made available to this
        # customer.
        qs = queryset.filter(
            Q(availability__isnull=True) | Q(id__in=available_role_ids)
        )
        # Concealed roles are excluded by default (pickers), but kept when
        # include_concealed is set so the staff management view can list them.
        include_concealed = str(self.data.get("include_concealed", "")).lower() in (
            "true",
            "1",
        )
        if not include_concealed:
            concealed_role_ids = models.CustomerRoleConcealment.objects.filter(
                content_type=customer_ct, object_id=customer.id
            ).values_list("role_id", flat=True)
            qs = qs.exclude(id__in=concealed_role_ids)
        return qs.distinct()

    class Meta:
        model = models.Role
        fields = [
            "is_active",
            "is_system_role",
            "name",
            "description",
            "include_concealed",
        ]


class CustomerRoleConcealmentFilter(django_filters.FilterSet):
    role_uuid = core_filters.RelatedUUIDFilter(
        view_name="role-detail",
        field_name="role__uuid",
        label="Role UUID",
    )
    scope_uuid = django_filters.UUIDFilter(
        method="filter_customer", label="Customer (scope) UUID"
    )

    def filter_customer(self, queryset, name, value):
        try:
            customer = Customer.objects.get(uuid=value)
        except Customer.DoesNotExist:
            return queryset.none()
        customer_ct = ContentType.objects.get_for_model(Customer)
        return queryset.filter(content_type=customer_ct, object_id=customer.id)

    class Meta:
        model = models.CustomerRoleConcealment
        fields = ["role_uuid", "scope_uuid"]


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
    is_active = django_filters.BooleanFilter(widget=BooleanWidget)
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
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        method="filter_customer_uuid",
        label="Grants within a customer (uuid): the customer scope plus its projects",
    )

    def filter_by_full_name(self, queryset, name, value):
        return filter_by_full_name(queryset, value, "user")

    def filter_scope_type(self, queryset, name, value):
        return queryset.filter(content_type__model=value)

    def filter_customer_uuid(self, queryset, name, value):
        try:
            customer = Customer.objects.get(uuid=value)
        except Customer.DoesNotExist:
            return queryset.none()
        customer_ct = ContentType.objects.get_for_model(Customer)
        project_ct = ContentType.objects.get_for_model(Project)
        project_ids = Project.objects.filter(customer=customer).values_list(
            "id", flat=True
        )
        return queryset.filter(
            Q(content_type=customer_ct, object_id=customer.id)
            | Q(content_type=project_ct, object_id__in=project_ids)
        )

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
            "is_active",
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
            "customer_uuid",
            "o",
        ]
