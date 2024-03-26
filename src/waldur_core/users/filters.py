import django_filters
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from rest_framework.filters import BaseFilterBackend

from waldur_core.core import filters as core_filters
from waldur_core.core.models import User
from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.permissions.utils import (
    get_create_permission,
    get_scope_ids,
    get_valid_content_types,
    get_valid_models,
)
from waldur_core.structure.managers import get_connected_customers

from . import models


class InvitationScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def get_related_models(self):
        return get_valid_models()

    def get_field_name(self):
        return "scope"


class InvitationFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user: User = request.user

        if user.is_staff or user.is_support:
            return queryset

        subquery = Q(customer__in=get_connected_customers(user))
        for content_type in get_valid_content_types():
            permission = get_create_permission(content_type.model_class())
            if not permission:
                continue
            scopes = get_scope_ids(user, content_type, permission=permission)
            subquery |= Q(content_type=content_type, object_id__in=scopes)

        return queryset.filter(subquery)


class BaseInvitationFilter(django_filters.FilterSet):
    role_uuid = django_filters.UUIDFilter(field_name="role__uuid")
    customer_uuid = django_filters.UUIDFilter(field_name="customer__uuid")
    scope_type = django_filters.CharFilter(method="filter_by_scope_type")

    class Meta:
        model = models.BaseInvitation
        fields = [
            "customer_uuid",
            "role_uuid",
        ]

    def filter_by_scope_type(self, queryset, name, value):
        if value in TYPE_MAP:
            ctype = ContentType.objects.get_by_natural_key(*TYPE_MAP[value])
            return queryset.filter(content_type=ctype)


class GroupInvitationFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user: User = request.user

        if user.is_staff or user.is_support:
            return queryset

        if view.detail:
            return queryset

        return queryset.filter(customer_id__in=get_connected_customers(user))


class GroupInvitationFilter(BaseInvitationFilter):
    o = django_filters.OrderingFilter(fields=("created",))

    class Meta:
        model = models.GroupInvitation
        fields = BaseInvitationFilter.Meta.fields + ["is_active"]


class InvitationFilter(BaseInvitationFilter):
    state = django_filters.MultipleChoiceFilter(choices=models.Invitation.State.CHOICES)
    email = django_filters.CharFilter(lookup_expr="icontains")

    o = django_filters.OrderingFilter(fields=("email", "state", "created"))

    class Meta:
        model = models.Invitation
        fields = BaseInvitationFilter.Meta.fields + ["email", "state", "civil_number"]


class PermissionRequestScopeFilterBackend(InvitationScopeFilterBackend):
    content_type_field = "invitation__content_type"
    object_id_field = "invitation__object_id"


class PermissionRequestFilter(django_filters.FilterSet):
    state = core_filters.ReviewStateFilter()
    customer_uuid = django_filters.UUIDFilter(field_name="invitation__customer__uuid")
    invitation = django_filters.UUIDFilter(field_name="invitation__uuid")
    created_by = django_filters.UUIDFilter(field_name="created_by__uuid")
    o = django_filters.OrderingFilter(fields=("state", "created"))

    class Meta:
        model = models.PermissionRequest
        fields = [
            "state",
            "invitation",
        ]


class PendingInvitationFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        queryset = queryset.filter(state=models.Invitation.State.PENDING)
        queryset = queryset.filter(
            Q(civil_number="") | Q(civil_number=request.user.civil_number)
        )
        if settings.WALDUR_CORE["VALIDATE_INVITATION_EMAIL"]:
            queryset = queryset.filter(email=request.user.email)

        return queryset
