import django_filters
from django.conf import settings
from django.db.models import Q
from rest_framework.filters import BaseFilterBackend

from waldur_core.core import filters as core_filters
from waldur_core.core.utils import is_uuid_like
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)
from waldur_core.users import models


class InvitationFilter(django_filters.FilterSet):
    project = django_filters.UUIDFilter(
        field_name="project__uuid",
    )
    project_url = core_filters.URLFilter(
        view_name="project-detail",
        field_name="project__uuid",
    )
    state = django_filters.MultipleChoiceFilter(choices=models.Invitation.State.CHOICES)
    email = django_filters.CharFilter(lookup_expr="icontains")

    o = django_filters.OrderingFilter(fields=("email", "state", "created"))

    class Meta:
        model = models.Invitation
        fields = [
            "email",
            "civil_number",
            "customer_role",
            "project_role",
        ]


class GroupInvitationFilter(django_filters.FilterSet):
    project = django_filters.UUIDFilter(
        field_name="project__uuid",
    )
    project_url = core_filters.URLFilter(
        view_name="project-detail",
        field_name="project__uuid",
    )
    customer = django_filters.UUIDFilter(
        field_name="customer__uuid",
    )
    customer_url = core_filters.URLFilter(
        view_name="customer-detail",
        field_name="customer__uuid",
    )

    o = django_filters.OrderingFilter(fields=("state", "created"))

    class Meta:
        model = models.GroupInvitation
        fields = [
            "customer_role",
            "project_role",
            "is_active",
        ]


class PermissionRequestFilter(django_filters.FilterSet):
    state = core_filters.ReviewStateFilter()
    project = django_filters.UUIDFilter(field_name="invitation__project__uuid")
    customer = django_filters.UUIDFilter(field_name="invitation__customer__uuid")
    invitation = django_filters.UUIDFilter(field_name="invitation__uuid")
    created_by = django_filters.UUIDFilter(field_name="created_by__uuid")
    o = django_filters.OrderingFilter(fields=("state", "created"))

    class Meta:
        model = models.PermissionRequest
        fields = [
            "state",
            "customer",
            "project",
            "invitation",
        ]


class InvitationCustomerFilterBackend(BaseFilterBackend):
    url_filter = core_filters.URLFilter(
        view_name="customer-detail",
        field_name="customer__uuid",
    )

    def filter_queryset(self, request, queryset, view):
        user = request.user

        if not (user.is_staff or user.is_support):
            customer_ids = get_connected_customers(user)
            project_ids = get_connected_projects(user, RoleEnum.PROJECT_MANAGER)
            queryset = queryset.filter(
                Q(customer__in=customer_ids) | Q(project__in=project_ids)
            ).distinct()

        customer_uuid = self.extract_customer_uuid(request)
        if not customer_uuid:
            return queryset

        if not is_uuid_like(customer_uuid):
            return queryset.none()

        query = Q(customer__uuid=customer_uuid)
        query |= Q(project__customer__uuid=customer_uuid)
        return queryset.filter(query)

    def extract_customer_uuid(self, request):
        if "customer_url" in request.query_params:
            return self.url_filter.get_uuid(request.query_params["customer_url"])

        if "customer" in request.query_params:
            return request.query_params["customer"]


class PendingInvitationFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        queryset = queryset.filter(state=models.Invitation.State.PENDING)
        queryset = queryset.filter(
            Q(civil_number="") | Q(civil_number=request.user.civil_number)
        )
        if settings.WALDUR_CORE["VALIDATE_INVITATION_EMAIL"]:
            queryset = queryset.filter(email=request.user.email)

        return queryset
