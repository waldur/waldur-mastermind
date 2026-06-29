import django_filters
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from rest_framework.filters import BaseFilterBackend

from waldur_core.core import filters as core_filters
from waldur_core.core.models import User
from waldur_core.permissions.enums import TYPE_MAP, PermissionEnum
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import (
    get_create_permission,
    get_scope_ids,
    get_valid_content_types,
    get_valid_models,
)
from waldur_core.structure.managers import (
    get_connected_customers_by_permission,
)
from waldur_core.users.enums import InvitationState

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

        subquery = Q(
            customer__in=get_connected_customers_by_permission(
                user, PermissionEnum.LIST_INVITATIONS
            )
        ) | Q(
            email__iexact=user.email,
            state__in=[InvitationState.PENDING, InvitationState.PENDING_PROJECT],
        )
        for content_type in get_valid_content_types():
            permission = get_create_permission(content_type.model_class())
            if not permission:
                continue
            scopes = get_scope_ids(user, content_type, permission=permission)
            subquery |= Q(content_type=content_type, object_id__in=scopes)

        return queryset.filter(subquery).distinct()


class BaseInvitationFilter(django_filters.FilterSet):
    role_uuid = core_filters.RelatedUUIDFilter(
        view_name="role-detail", field_name="role__uuid"
    )
    role_name = django_filters.CharFilter(field_name="role__name")
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="customer__uuid"
    )
    scope_type = django_filters.CharFilter(method="filter_by_scope_type")

    class Meta:
        model = models.BaseInvitation
        fields = [
            "customer_uuid",
            "role_uuid",
            "role_name",
        ]

    def filter_by_scope_type(self, queryset, name, value):
        if value in TYPE_MAP:
            ctype = ContentType.objects.get_by_natural_key(*TYPE_MAP[value])
            return queryset.filter(content_type=ctype)


class GroupInvitationFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user: User = request.user

        # Staff/support users can see everything
        if user.is_authenticated and (user.is_staff or user.is_support):
            return queryset

        # Unauthenticated users can only see public active invitations
        if not user.is_authenticated:
            return queryset.filter(is_public=True, is_active=True)

        # For authenticated non-staff users
        if view.detail:
            # For detail views, let users access invitations they have permission to see
            # or public invitations - don't filter too restrictively here
            return queryset

        # For list views, show user's accessible invitations + public ones
        user_invitations = queryset.filter(
            customer_id__in=get_connected_customers_by_permission(
                user, PermissionEnum.LIST_INVITATIONS
            )
        )
        public_invitations = queryset.filter(is_public=True, is_active=True)

        return (user_invitations | public_invitations).distinct()


class GroupInvitationFilter(BaseInvitationFilter):
    o = django_filters.OrderingFilter(fields=("created",))

    class Meta:
        model = models.GroupInvitation
        fields = BaseInvitationFilter.Meta.fields + ["is_active", "is_public"]


class InvitationFilter(BaseInvitationFilter):
    state = django_filters.MultipleChoiceFilter(choices=InvitationState.choices)
    email = django_filters.CharFilter(lookup_expr="icontains")
    email_exact = django_filters.CharFilter(lookup_expr="iexact", field_name="email")
    scope_name = django_filters.CharFilter(method="filter_by_scope_name")
    scope_description = django_filters.CharFilter(method="filter_by_scope_description")

    o = django_filters.OrderingFilter(
        fields=("email", "state", "created_by", "created")
    )

    class Meta:
        model = models.Invitation
        fields = BaseInvitationFilter.Meta.fields + [
            "email",
            "state",
            "civil_number",
            "scope_name",
            "scope_description",
        ]

    def filter_by_scope_name(self, queryset, name, value):
        """Filter invitations by scope name (case-insensitive contains)."""
        if not value:
            return queryset

        # Build a list of invitation IDs that match the scope name criteria
        matching_invitation_ids = []

        # Get all valid content types for scopes
        for content_type in get_valid_content_types():
            model_class = content_type.model_class()
            if not model_class:
                continue

            # Check if the model has a 'name' field by examining its _meta fields
            field_names = [f.name for f in model_class._meta.get_fields()]
            if "name" in field_names:
                try:
                    # Find all objects of this type that match the name criteria
                    matching_scope_ids = list(
                        model_class.objects.filter(name__icontains=value).values_list(
                            "id", flat=True
                        )
                    )

                    if matching_scope_ids:
                        # Find invitations that reference these scope objects
                        invitation_ids = list(
                            queryset.filter(
                                content_type=content_type,
                                object_id__in=matching_scope_ids,
                            ).values_list("id", flat=True)
                        )
                        matching_invitation_ids.extend(invitation_ids)
                except Exception:
                    # Skip models that cause database errors
                    continue

        # Return filtered queryset
        if matching_invitation_ids:
            return queryset.filter(id__in=matching_invitation_ids)
        else:
            return queryset.none()

    def filter_by_scope_description(self, queryset, name, value):
        """Filter invitations by scope description (case-insensitive contains)."""
        if not value:
            return queryset

        # Build a list of invitation IDs that match the scope description criteria
        matching_invitation_ids = []

        # Get all valid content types for scopes
        for content_type in get_valid_content_types():
            model_class = content_type.model_class()
            if not model_class:
                continue

            # Check if the model has a 'description' field by examining its _meta fields
            field_names = [f.name for f in model_class._meta.get_fields()]
            if "description" in field_names:
                try:
                    # Find all objects of this type that match the description criteria
                    matching_scope_ids = list(
                        model_class.objects.filter(
                            description__icontains=value
                        ).values_list("id", flat=True)
                    )

                    if matching_scope_ids:
                        # Find invitations that reference these scope objects
                        invitation_ids = list(
                            queryset.filter(
                                content_type=content_type,
                                object_id__in=matching_scope_ids,
                            ).values_list("id", flat=True)
                        )
                        matching_invitation_ids.extend(invitation_ids)
                except Exception:
                    # Skip models that cause database errors
                    continue

        # Return filtered queryset
        if matching_invitation_ids:
            return queryset.filter(id__in=matching_invitation_ids)
        else:
            return queryset.none()


class PermissionRequestScopeFilterBackend(InvitationScopeFilterBackend):
    content_type_field = "invitation__content_type"
    object_id_field = "invitation__object_id"


class PermissionRequestFilter(django_filters.FilterSet):
    state = core_filters.ReviewStateFilter()
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="invitation__customer__uuid"
    )
    invitation = core_filters.RelatedUUIDFilter(
        view_name="user-invitation-detail", field_name="invitation__uuid"
    )
    created_by = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="created_by__uuid"
    )
    o = django_filters.OrderingFilter(fields=("state", "created"))

    class Meta:
        model = models.PermissionRequest
        fields = [
            "state",
            "invitation",
        ]


def filter_invitations_for_recipient(user, states):
    subquery = Q(state__in=states) & (
        Q(civil_number="") | Q(civil_number=user.civil_number)
    )
    if settings.WALDUR_CORE["VALIDATE_INVITATION_EMAIL"]:
        subquery = subquery & Q(email=user.email)

    return subquery


def filter_pending_invitations(user):
    return filter_invitations_for_recipient(user, [InvitationState.PENDING])


def filter_inactive_invitations_for_recipient(user):
    return filter_invitations_for_recipient(
        user, [InvitationState.EXPIRED, InvitationState.CANCELED]
    )


def filter_by_connected_scopes(user):
    subquery = Q()

    for user_role in UserRole.objects.filter(user=user, is_active=True):
        subquery |= Q(
            content_type=user_role.content_type, object_id=user_role.object_id
        )
    return subquery


class PendingInvitationFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        subquery = filter_pending_invitations(request.user)
        return queryset.filter(subquery)


class VisibleInvitationFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if request.user.is_staff or request.user.is_support:
            return queryset
        subquery1 = filter_pending_invitations(request.user)
        subquery2 = filter_by_connected_scopes(request.user)
        subquery3 = filter_inactive_invitations_for_recipient(request.user)
        return queryset.filter(subquery1 | subquery2 | subquery3)
