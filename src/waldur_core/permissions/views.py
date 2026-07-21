import logging
import uuid

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Q, QuerySet, Value
from django.db.models.functions import Coalesce, Lower, NullIf
from django.utils.translation import gettext_lazy as _
from django_filters import utils as django_filters_utils
from django_filters.rest_framework.backends import DjangoFilterBackend
from drf_spectacular.plumbing import (
    OpenApiTypes,
    build_array_type,
    build_basic_type,
)
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from waldur_core.core.models import User
from waldur_core.core.permissions import IsAdminOrReadOnly
from waldur_core.core.utils import get_ip_address, is_uuid_like
from waldur_core.core.views import ActionsViewSet, count_action
from waldur_core.permissions.filters import UserPermissionFilter
from waldur_core.permissions.utils import (
    add_user,
    delete_user,
    get_create_permission,
    get_delete_permission,
    get_permissions,
    has_permission,
    update_user,
)
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)
from waldur_core.structure.permissions import _get_customer

from . import enums, filters, models, serializers

logger = logging.getLogger(__name__)


def can_destroy_role(role):
    if role.is_system_role:
        raise ValidationError("Destroying of system role is not available.")
    if models.UserRole.objects.filter(is_active=True, role=role).exists():
        raise ValidationError("Role is still used.")


def validate_scope_not_soft_deleted(scope):
    """Validate that the scope is not a soft-deleted project."""
    if (
        scope._meta.model_name == "project"
        and hasattr(scope, "is_removed")
        and scope.is_removed
    ):
        raise ValidationError(
            {
                "non_field_errors": [
                    "Cannot manage team members for terminated projects."
                ]
            }
        )


@extend_schema_view(
    list=extend_schema(
        summary="List roles",
        description="Get a list of all available roles.",
    ),
    retrieve=extend_schema(
        summary="Get role details",
        description="Retrieve the details of a specific role by its UUID.",
    ),
    destroy=extend_schema(
        summary="Delete a role",
        description="Allows staff users to delete a custom role. System roles and roles that are currently in use cannot be deleted.",
    ),
)
class RoleViewSet(ActionsViewSet):
    queryset = models.Role.objects.all()
    serializer_class = serializers.RoleDetailsSerializer
    lookup_field = "uuid"
    permission_classes = [IsAdminOrReadOnly]
    filterset_class = filters.RoleFilter
    destroy_validators = [can_destroy_role]

    def get_queryset(self):
        # Sort/search operate on the displayed name (the description, falling
        # back to the machine name), so ordering by "name" groups roles the way
        # the UI shows them (e.g. an org clone next to the system role it copies).
        qs = (
            super()
            .get_queryset()
            .annotate(
                display_name=Lower(Coalesce(NullIf("description", Value("")), "name")),
                # Active assignment count, exposed for server-side ordering.
                assigned_users_count=Count(
                    "userrole", filter=Q(userrole__is_active=True)
                ),
            )
        )
        user = self.request.user
        if getattr(user, "is_authenticated", False) and (
            user.is_staff or user.is_support
        ):
            return qs
        # A non-staff user must not see other organizations' private roles (which
        # would leak the org's existence and role structure). Hide only
        # customer-scoped roles bound to an organization the user does not belong
        # to; public/system roles and offering-scoped roles stay visible (an
        # anonymous user sees the public roles, none of the org-private ones).
        customer_ids = set()
        if getattr(user, "is_authenticated", False):
            customer_ids.update(get_connected_customers(user))
            customer_ids.update(
                structure_models.Project.objects.filter(
                    id__in=get_connected_projects(user)
                ).values_list("customer_id", flat=True)
            )
        customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
        customer_scoped = models.RoleAvailability.objects.filter(
            content_type=customer_ct
        ).values_list("role_id", flat=True)
        mine = models.RoleAvailability.objects.filter(
            content_type=customer_ct, object_id__in=customer_ids
        ).values_list("role_id", flat=True)
        return qs.exclude(Q(id__in=customer_scoped) & ~Q(id__in=mine)).distinct()

    @extend_schema(
        summary="Create a new role",
        description="Allows staff users to create a new custom role with a specific set of permissions.",
        request=serializers.RoleModifySerializer,
        responses=serializers.RoleDetailsSerializer,
        examples=[
            OpenApiExample(
                "Create a project-level reviewer role",
                request_only=True,
                value={
                    "name": "PROJECT.REVIEWER",
                    "description": "Can view project.",
                    "content_type": "project",
                    "permissions": ["PROJECT.LIST"],
                },
            ),
            OpenApiExample(
                "Successful role creation response",
                response_only=True,
                status_codes=[status.HTTP_201_CREATED],
                value={
                    "uuid": "d8c2d5852d434937985392d24249b6d3",
                    "name": "PROJECT.REVIEWER",
                    "description": "Can view project.",
                    "permissions": ["PROJECT.LIST"],
                    "is_system_role": False,
                    "is_active": True,
                    "users_count": 0,
                    "content_type": "project",
                },
            ),
        ],
    )
    def create(self, request):
        serializer = serializers.RoleModifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role: models.Role = serializer.save()
        serializer = serializers.RoleDetailsSerializer(instance=role)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Update a role",
        description="Allows staff users to update an existing role's name, description, content type, and permissions. The name of a system role cannot be changed.",
        request=serializers.RoleModifySerializer,
        responses=serializers.RoleDetailsSerializer,
        examples=[
            OpenApiExample(
                "Add a permission to a role",
                request_only=True,
                value={
                    "name": "PROJECT.REVIEWER",
                    "description": "Can view project and update metadata.",
                    "content_type": "project",
                    "permissions": ["PROJECT.LIST", "PROJECT.UPDATE_METADATA"],
                },
            ),
            OpenApiExample(
                "Successful role update response",
                response_only=True,
                status_codes=[status.HTTP_200_OK],
                value={
                    "uuid": "d8c2d5852d434937985392d24249b6d3",
                    "name": "PROJECT.REVIEWER",
                    "description": "Can view project and update metadata.",
                    "permissions": ["PROJECT.LIST", "PROJECT.UPDATE_METADATA"],
                    "is_system_role": False,
                    "is_active": True,
                    "users_count": 5,
                    "content_type": "project",
                },
            ),
        ],
    )
    def update(self, request, **kwargs):
        instance: models.Role = self.get_object()
        serializer = serializers.RoleModifySerializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        role: models.Role = serializer.save()
        serializer = serializers.RoleDetailsSerializer(instance=role)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Update role descriptions",
        description="Allows staff users to update the multilingual descriptions of a role.",
        request=serializers.RoleDescriptionSerializer,
        responses=serializers.RoleDescriptionSerializer,
        examples=[
            OpenApiExample(
                "Update English and Estonian descriptions",
                value={
                    "description_en": "New English description",
                    "description_et": "Uus kirjeldus eesti keeles",
                },
            )
        ],
    )
    @action(detail=True, methods=["PUT"])
    def update_descriptions(self, request, uuid=None):
        instance: models.Role = self.get_object()
        serializer = serializers.RoleDescriptionSerializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Enable a role",
        description="Allows staff users to enable a role, making it available for assignment.",
        request=None,
        responses={200: OpenApiResponse(description="Role enabled successfully.")},
        examples=[
            OpenApiExample(
                "Successful role enable response",
                response_only=True,
                value={"detail": "The role PROJECT.REVIEWER has been enabled"},
            )
        ],
    )
    @action(detail=True, methods=["post"])
    def enable(self, request, uuid=None):
        role: models.Role = self.get_object()
        message = f"The role {role.name} has been enabled"
        if not role.is_active:
            role.is_active = True
            role.save()
            logger.info(message)
        return Response(
            {"detail": _(message)},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Disable a role",
        description="Allows staff users to disable a role, preventing it from being assigned further. Existing assignments are not affected.",
        request=None,
        responses={200: OpenApiResponse(description="Role disabled successfully.")},
        examples=[
            OpenApiExample(
                "Successful role disable response",
                response_only=True,
                value={"detail": "The role PROJECT.REVIEWER has been disabled"},
            )
        ],
    )
    @action(detail=True, methods=["post"])
    def disable(self, request, uuid=None):
        role: models.Role = self.get_object()
        message = f"The role {role.name} has been disabled"
        if role.is_active:
            role.is_active = False
            role.save()
            logger.info(message)
        return Response(
            {"detail": _(message)},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Clone a role into an organization",
        description="Staff-only. Creates an organization-private copy of this "
        "role (customer or project scope), bound to the given customer and "
        "usable only within that organization and its projects. Cloning the "
        "same template into the same organization twice is rejected.",
        request=serializers.RoleCloneSerializer,
        responses=serializers.RoleDetailsSerializer,
    )
    @action(detail=True, methods=["post"])
    def clone_to_customer(self, request, uuid=None):
        template: models.Role = self.get_object()
        serializer = serializers.RoleCloneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role = serializers.clone_role_for_customer(
            template,
            serializer.validated_data["customer"],
            serializer.validated_data.get("description"),
            conceal_template=serializer.validated_data["conceal_template"],
        )
        logger.info(
            "Role %s cloned into customer %s as %s",
            template.name,
            serializer.validated_data["customer"].uuid.hex,
            role.name,
        )
        return Response(
            serializers.RoleDetailsSerializer(role, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    clone_to_customer_serializer_class = serializers.RoleCloneSerializer


class CustomerRoleConcealmentViewSet(ActionsViewSet):
    """Staff-only management of per-organization role concealments (deny-list).

    Concealing a role hides it from the organization's role pickers and blocks
    new grants of it within the organization; existing grants are untouched.
    Deleting a concealment reveals the role again. A lockout guard refuses to
    conceal the last role that can grant access at its own scope.
    """

    queryset = models.CustomerRoleConcealment.objects.select_related(
        "role", "content_type"
    ).order_by("role__name")
    serializer_class = serializers.CustomerRoleConcealmentSerializer
    lookup_field = "uuid"
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CustomerRoleConcealmentFilter
    disabled_actions = ["update", "partial_update"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not (user and user.is_authenticated and user.is_staff):
            return qs.none()
        return qs


class RoleAvailabilityViewSet(ActionsViewSet):
    """Staff-only audit / cleanup view over RoleAvailability rows.

    Day-to-day binding management happens elsewhere — through
    OfferingProfile.add_role / remove_role for profile-managed catalogs,
    and through OfferingRoleViewSet for direct per-offering rows. This
    endpoint is for staff to audit the raw bindings (e.g. spot orphans
    left over from manual API calls) and to delete a row out-of-band when
    needed.
    """

    queryset = models.RoleAvailability.objects.select_related(
        "role", "content_type"
    ).order_by("role__name")
    serializer_class = serializers.RoleAvailabilityDetailsSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.RoleAvailabilityFilter
    disabled_actions = ["create", "update", "partial_update"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not (user and user.is_authenticated and user.is_staff):
            return qs.none()
        return qs


def _user_can_view_scope_team(user, scope) -> bool:
    """Authorise ``UserRoleMixin.list_users`` against a single scope.

    Returns True iff ``user`` is allowed to enumerate the membership and PII
    of users with roles on ``scope``. The check has two independent paths;
    either is sufficient.

    Consumer-side (any scope type)
        Staff / support, or a caller holding any active ``UserRole`` on the
        scope tree: the scope itself, its enclosing customer, or any project
        under that customer. Resolved in a single SQL EXISTS.

    Provider-side (only Resource / ResourceProject)
        A caller holding ``UPDATE_OFFERING`` on the offering selling this
        scope, or on the offering's customer. This covers
        ``OFFERING.MANAGER`` (offering-scoped), ``CUSTOMER.OWNER`` and
        ``SERVICE_PROVIDER.MANAGER`` (customer-scoped) on the provider side.
        Resolved in a single SQL EXISTS that mirrors the consumer-side query.

    Returns False for ``Project`` / ``Customer`` scopes that fail the
    consumer-side check (the provider-side branch is short-circuited via
    ``_get_offering_for_scope`` returning ``None``).
    """
    if not user or user.is_anonymous:
        return False
    if user.is_staff or user.is_support:
        return True

    # Consumer-side: UserRole on the scope, its customer, or any project
    # under that customer.
    scope_meta = scope._meta
    q = Q(
        content_type__app_label=scope_meta.app_label,
        content_type__model=scope_meta.model_name,
        object_id=scope.id,
    )
    customer = _get_customer(scope)
    if customer is not None:
        q |= Q(
            content_type__app_label="structure",
            content_type__model="customer",
            object_id=customer.id,
        )
        q |= Q(
            content_type__app_label="structure",
            content_type__model="project",
            object_id__in=structure_models.Project.objects.filter(
                customer_id=customer.id
            ).values_list("id", flat=True),
        )
    if models.UserRole.objects.filter(user=user, is_active=True).filter(q).exists():
        return True

    # Provider-side: caller manages the offering selling this scope.
    # Only Resource and ResourceProject scopes have an offering — Project
    # and Customer scopes fall through and the function returns False.
    # Accept UPDATE_OFFERING on EITHER the offering itself (OFFERING.MANAGER
    # role) OR the offering's customer (CUSTOMER.OWNER /
    # SERVICE_PROVIDER.MANAGER roles). Folded into a single SQL EXISTS to
    # mirror the consumer-side query above.
    offering = _get_offering_for_scope(scope)
    if offering is None:
        return False
    offering_ct = ContentType.objects.get_for_model(type(offering))
    provider_q = Q(content_type=offering_ct, object_id=offering.id)
    if offering.customer_id:
        customer_ct = ContentType.objects.get_for_model(type(offering.customer))
        provider_q |= Q(content_type=customer_ct, object_id=offering.customer_id)
    return (
        models.UserRole.objects.filter(
            user=user,
            is_active=True,
            role__permissions__permission=enums.PermissionEnum.UPDATE_OFFERING.value,
        )
        .filter(provider_q)
        .exists()
    )


def _get_offering_for_scope(scope):
    """Return the marketplace.Offering that sells this scope, or None.

    For ``Resource`` the offering is direct; for ``ResourceProject`` it's
    one hop through ``resource``. Other scope types have no provider-side
    offering in the marketplace sense.

    Duck-typed on purpose — ``waldur_core.permissions`` cannot import from
    ``waldur_mastermind.marketplace`` without violating the core/mastermind
    layering. The assumption "any scope with a `.offering` attribute is a
    marketplace Resource (or with `.resource.offering`, a ResourceProject)"
    holds today; if a future scope type adds an unrelated `.offering`
    attribute, this gate would falsely accept its callers.
    """
    offering = getattr(scope, "offering", None)
    if offering is None:
        resource = getattr(scope, "resource", None)
        if resource is not None:
            offering = getattr(resource, "offering", None)
    return offering


class UserRoleMixin:
    """Mixin to provide user role management functionality for viewsets."""

    def can_view_scope_team(self, user, scope) -> bool:
        """Authorise list_users / team listing for ``scope``.

        Override in a viewset to add scope-specific authorisation paths
        (e.g. a role held on a parent object the core walk does not reach).
        """
        return _user_can_view_scope_team(user, scope)

    @extend_schema(
        summary="List users and their roles in a scope",
        description="Retrieves a list of users who have a role within a specific scope (e.g., a project or an organization). The list can be filtered by user details or role.",
        parameters=[
            OpenApiParameter(
                name="user",
                type=uuid.UUID,
                location=OpenApiParameter.QUERY,
                description="User UUID",
                extensions={"x-waldur-operation-id": "users_retrieve"},
            ),
            OpenApiParameter(
                name="user_url",
                type=str,
                location=OpenApiParameter.QUERY,
                description="User URL",
            ),
            OpenApiParameter(
                name="username",
                type=str,
                location=OpenApiParameter.QUERY,
                description="User username",
            ),
            OpenApiParameter(
                name="full_name",
                type=str,
                location=OpenApiParameter.QUERY,
                description="User full name",
            ),
            OpenApiParameter(
                name="native_name",
                type=str,
                location=OpenApiParameter.QUERY,
                description="User native name",
            ),
            OpenApiParameter(
                name="user_slug",
                type=str,
                location=OpenApiParameter.QUERY,
                description="User slug",
            ),
            OpenApiParameter(
                name="role",
                type=build_array_type(build_basic_type(OpenApiTypes.STR)),
                location=OpenApiParameter.QUERY,
                description="Role UUID or name. Repeat to filter by several roles.",
                extensions={"x-waldur-operation-id": "roles_retrieve"},
            ),
            OpenApiParameter(
                name="search_string",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Search string for user",
            ),
            OpenApiParameter(
                "field",
                build_array_type(build_basic_type(OpenApiTypes.STR)),
                OpenApiParameter.QUERY,
                enum=serializers.UserRoleDetailsSerializer.Meta.fields,
                description="Fields to include in response",
            ),
            OpenApiParameter(
                "o",
                build_array_type(build_basic_type(OpenApiTypes.STR)),
                OpenApiParameter.QUERY,
                enum=[
                    "username",
                    "full_name",
                    "native_name",
                    "email",
                    "expiration_time",
                    "created",
                    "role",
                ],
                description="Ordering fields",
            ),
        ],
        responses=serializers.UserRoleDetailsSerializer(many=True),
        filters=False,
        examples=[
            OpenApiExample(
                "Example user role list response",
                response_only=True,
                value=[
                    {
                        "uuid": "a7e6b0a29a434d2889273c5240217a26",
                        "created": "2023-10-26T10:00:00Z",
                        "expiration_time": "2024-12-31T23:59:59Z",
                        "role_name": "PROJECT.ADMIN",
                        "role_uuid": "d8c2d5852d434937985392d24249b6d3",
                        "user_email": "alice@example.com",
                        "user_full_name": "Alice Smith",
                        "user_username": "alice",
                        "user_uuid": "8f20242b638743b18a485f81ac685e13",
                        "user_image": None,
                        "created_by_full_name": "Bob Johnson",
                        "created_by_uuid": "b4e4c2f1a6f24a8d8e4c2f1a6f24a8d8",
                    }
                ],
            )
        ],
    )
    @count_action
    @action(detail=True, methods=["GET"])
    def list_users(self, request, uuid=None):
        scope = self.get_object()
        if not self.can_view_scope_team(request.user, scope):
            raise PermissionDenied(
                "You do not have permission to list team members of this scope."
            )
        user_uuid = request.query_params.get("user")

        user = None
        if user_uuid and is_uuid_like(user_uuid):
            try:
                user = User.objects.get(uuid=user_uuid)
            except User.DoesNotExist:
                pass
        if hasattr(self, "get_user_roles_queryset"):
            queryset = self.get_user_roles_queryset(scope, user)
        else:
            queryset = get_permissions(scope, user)

        kwargs = DjangoFilterBackend().get_filterset_kwargs(request, queryset, self)
        filterset = UserPermissionFilter(**kwargs)

        if not filterset.is_valid():
            raise django_filters_utils.translate_validation(filterset.errors)

        queryset = filterset.qs

        # A scope's Team tab may request several roles at once (e.g. the call
        # team lists both call managers and panel members), sent as a repeated
        # "role" query param. getlist honours all of them; a single value is
        # simply a list of one, so callers passing one role are unaffected.
        roles = request.query_params.getlist("role")
        search_string = request.query_params.get("search_string")
        if search_string:
            queryset = queryset.filter(
                Q(user__first_name__icontains=search_string)
                | Q(user__last_name__icontains=search_string)
                | Q(user__email__icontains=search_string)
                | Q(user__username__icontains=search_string)
            ).distinct()
        if roles:
            uuid_roles = [role for role in roles if is_uuid_like(role)]
            name_roles = [role for role in roles if not is_uuid_like(role)]
            role_query = Q()
            if uuid_roles:
                role_query |= Q(role__uuid__in=uuid_roles)
            if name_roles:
                role_query |= Q(role__name__in=name_roles)
            queryset = queryset.filter(role_query)
        queryset = self.paginate_queryset(queryset)
        if request.method == "HEAD":
            # Count-only request (the `_count` companion): the X-Result-Count
            # header is set from the paginator, so skip serialising the page.
            return self.get_paginated_response([])
        serializer = serializers.UserRoleDetailsSerializer(
            queryset, many=True, context={"request": request}
        )
        data = self.filter_user_roles_representation(serializer.data, scope, request)
        return self.get_paginated_response(data)

    def filter_user_roles_representation(self, data, scope, request):
        """Hook for subclasses to redact fields from the list_users payload
        (e.g. hide role expiration from reviewers). Default: no change."""
        return data

    @extend_schema(
        summary="Grant a role to a user",
        description="Assigns a specific role to a user within the current scope. An optional expiration time for the role can be set.",
        request=serializers.UserRoleCreateSerializer,
        responses={
            201: serializers.UserRoleExpirationTimeSerializer,
            400: OpenApiResponse(
                description="Validation error, for example when trying to add a user to a terminated project."
            ),
        },
        examples=[
            OpenApiExample(
                "Grant project admin role until end of year",
                request_only=True,
                value={
                    "user": "8f20242b638743b18a485f81ac685e13",
                    "role": "PROJECT.ADMIN",
                    "expiration_time": "2024-12-31",
                },
            ),
            OpenApiExample(
                "Successful role grant response",
                response_only=True,
                status_codes=[201],
                value={"expiration_time": "2024-12-31T00:00:00Z"},
            ),
            OpenApiExample(
                "Example validation error",
                response_only=True,
                status_codes=[400],
                value={
                    "non_field_errors": [
                        "Cannot manage team members for terminated projects."
                    ]
                },
            ),
        ],
    )
    @action(detail=True, methods=["POST"])
    def add_user(self, request, uuid=None):
        scope = self.get_object()
        validate_scope_not_soft_deleted(scope)

        serializer = serializers.UserRoleCreateSerializer(
            data=request.data, context={"scope": scope, "request": request}
        )
        serializer.is_valid(raise_exception=True)

        target_user = serializer.validated_data["user"]
        role = serializer.validated_data["role"]
        expiration_time = serializer.validated_data.get("expiration_time")

        perm = add_user(
            scope,
            target_user,
            role,
            created_by=request.user,
            expiration_time=expiration_time,
        )
        return Response(
            status=status.HTTP_201_CREATED,
            data={"expiration_time": perm.expiration_time},
        )

    @extend_schema(
        summary="Update a user's role expiration",
        description="Updates the expiration time for a user's existing role in the current scope. This is useful for extending or shortening the duration of a permission. To make a role permanent, set expiration_time to null.",
        request=serializers.UserRoleUpdateSerializer,
        responses={200: serializers.UserRoleExpirationTimeSerializer},
        examples=[
            OpenApiExample(
                "Extend role until mid-2025",
                request_only=True,
                value={
                    "user": "8f20242b638743b18a485f81ac685e13",
                    "role": "PROJECT.ADMIN",
                    "expiration_time": "2025-06-30",
                },
            ),
            OpenApiExample(
                "Make a role permanent",
                request_only=True,
                value={
                    "user": "8f20242b638743b18a485f81ac685e13",
                    "role": "PROJECT.ADMIN",
                    "expiration_time": None,
                },
            ),
            OpenApiExample(
                "Successful role update response",
                response_only=True,
                status_codes=[200],
                value={"expiration_time": "2025-06-30T00:00:00Z"},
            ),
        ],
    )
    @action(detail=True, methods=["POST"])
    def update_user(self, request, uuid=None):
        scope = self.get_object()
        validate_scope_not_soft_deleted(scope)

        serializer = serializers.UserRoleUpdateSerializer(
            data=request.data, context={"scope": scope, "request": request}
        )
        serializer.is_valid(raise_exception=True)

        target_user = serializer.validated_data["user"]
        role = serializer.validated_data["role"]
        expiration_time = serializer.validated_data.get("expiration_time")

        perm = update_user(
            scope,
            target_user,
            role,
            expiration_time=expiration_time,
            current_user=request.user,
        )
        return Response(
            status=status.HTTP_200_OK, data={"expiration_time": perm.expiration_time}
        )

    @extend_schema(
        summary="Revoke a role from a user",
        description="Removes a specific role from a user within the current scope. This effectively revokes their permissions associated with that role.",
        request=serializers.UserRoleDeleteSerializer,
        responses={200: OpenApiResponse(description="Role revoked successfully.")},
        examples=[
            OpenApiExample(
                "Revoke project admin role from user",
                request_only=True,
                value={
                    "user": "8f20242b638743b18a485f81ac685e13",
                    "role": "PROJECT.ADMIN",
                },
            )
        ],
    )
    @action(detail=True, methods=["POST"])
    def delete_user(self, request, uuid=None):
        scope = self.get_object()
        validate_scope_not_soft_deleted(scope)

        serializer = serializers.UserRoleDeleteSerializer(
            data=request.data, context={"scope": scope, "request": request}
        )
        serializer.is_valid(raise_exception=True)

        target_user = serializer.validated_data["user"]
        role = serializer.validated_data["role"]

        delete_user(
            scope,
            target_user,
            role,
            request.user,
            reason="Manual user removal via delete_user API endpoint",
        )
        return Response(status=status.HTTP_200_OK)


def filter_user_permissions_by_ip_address(qs: QuerySet[models.UserRole], user_ip: str):
    from waldur_core.structure.managers import filter_customer_by_ip_address
    from waldur_core.structure.models import Customer, Project

    visible_customers_ids = filter_customer_by_ip_address(user_ip)
    invisible_customers_ids = Customer.objects.exclude(
        id__in=visible_customers_ids
    ).values_list("id", flat=True)
    invisible_project_ids = Project.objects.filter(
        customer_id__in=invisible_customers_ids
    ).values_list("id", flat=True)
    return qs.exclude(
        content_type=ContentType.objects.get_by_natural_key("structure", "customer"),
        object_id__in=invisible_customers_ids,
    ).exclude(
        content_type=ContentType.objects.get_by_natural_key("structure", "project"),
        object_id__in=invisible_project_ids,
    )


@extend_schema_view(
    list=extend_schema(
        summary="List user permissions",
        description="Get a list of all permissions for the current user. Staff and support users can view all user permissions. The list can be filtered by user, scope, role, etc. By default only active grants are returned; staff and support can pass show_inactive=true to include revoked grants (the full history).",
        parameters=[
            OpenApiParameter(
                "show_inactive",
                OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                description="Staff/support only. Include revoked (inactive) "
                "role grants in addition to active ones. Ignored for other "
                "users, who only ever see their own active roles.",
            ),
        ],
        examples=[
            OpenApiExample(
                "Example user permission list response",
                response_only=True,
                value=[
                    {
                        "user_uuid": "8f20242b638743b18a485f81ac685e13",
                        "user_name": "Alice Smith",
                        "user_slug": "alice-smith",
                        "created": "2023-10-26T10:00:00Z",
                        "expiration_time": "2024-12-31T23:59:59Z",
                        "created_by_full_name": "Bob Johnson",
                        "created_by_username": "bob",
                        "role_name": "PROJECT.ADMIN",
                        "role_description": "Project administrator with full control over the project.",
                        "role_uuid": "d8c2d5852d434937985392d24249b6d3",
                        "scope_type": "project",
                        "scope_uuid": "c81f33f1b4094a319e1fe1a3d5ca76a5",
                        "scope_name": "Cloud Storage Project",
                        "customer_uuid": "b7e4501a1c6a4f91807d79b932408c62",
                        "customer_name": "MegaCorp",
                    }
                ],
            )
        ],
    ),
    retrieve=extend_schema(
        summary="Get permission details",
        description="Retrieve the details of a specific user permission grant by its UUID.",
        examples=[
            OpenApiExample(
                "Example user permission detail response",
                response_only=True,
                value={
                    "user_uuid": "8f20242b638743b18a485f81ac685e13",
                    "user_name": "Alice Smith",
                    "user_slug": "alice-smith",
                    "created": "2023-10-26T10:00:00Z",
                    "expiration_time": "2024-12-31T23:59:59Z",
                    "created_by_full_name": "Bob Johnson",
                    "created_by_username": "bob",
                    "role_name": "PROJECT.ADMIN",
                    "role_description": "Project administrator with full control over the project.",
                    "role_uuid": "d8c2d5852d434937985392d24249b6d3",
                    "scope_type": "project",
                    "scope_uuid": "c81f33f1b4094a319e1fe1a3d5ca76a5",
                    "scope_name": "Cloud Storage Project",
                    "customer_uuid": "b7e4501a1c6a4f91807d79b932408c62",
                    "customer_name": "MegaCorp",
                },
            )
        ],
    ),
)
class UserPermissionViewSet(ReadOnlyModelViewSet):
    queryset = models.UserRole.objects.all()
    serializer_class = serializers.PermissionSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.UserPermissionFilter

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            # In the list, revoked (inactive) grants are hidden by default to
            # preserve the historical behaviour of this endpoint; opt in to the
            # full history (active + revoked) with show_inactive=true, and the
            # is_active filterset param can narrow it further if needed. Detail
            # lookups (retrieve plus the revoke/restore actions) always operate
            # on the full set so a revoked grant can be fetched and restored.
            show_inactive = self.request.query_params.get("show_inactive") == "true"
            if self.action == "list" and not show_inactive:
                qs = qs.filter(is_active=True)
            return qs
        # Other users only ever see their own currently active roles.
        qs = qs.filter(user=user, is_active=True).distinct()
        user_ip = get_ip_address(self.request)
        if not user_ip:
            return qs
        return filter_user_permissions_by_ip_address(qs, user_ip)

    def _check_action_permission(self, user_role, permission_getter):
        """Authorise revoke/restore of ``user_role`` mirroring the
        scope-based team management checks: the caller must hold the relevant
        permission on either the role's customer or the scope itself. Staff
        bypass via :func:`has_permission`."""
        scope = user_role.scope
        if scope is None:
            raise ValidationError("Role scope is not available.")
        permission = permission_getter(scope)
        customer = _get_customer(scope)
        if customer is not None and has_permission(self.request, permission, customer):
            return
        if has_permission(self.request, permission, scope):
            return
        raise PermissionDenied()

    @extend_schema(
        summary="Revoke a user role",
        description="Revokes a specific user role grant, deactivating the "
        "associated permissions immediately.",
        request=serializers.UserRolePermissionActionSerializer,
        responses={200: OpenApiResponse(description="Role revoked successfully.")},
    )
    @action(detail=True, methods=["post"])
    def revoke(self, request, uuid=None):
        user_role = self.get_object()
        serializer = serializers.UserRolePermissionActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self._check_action_permission(user_role, get_delete_permission)
        if not user_role.is_active:
            raise ValidationError("Role is already revoked.")
        reason = (
            serializer.validated_data.get("reason") or "Manual role removal via API"
        )
        user_role.revoke(current_user=request.user, reason=reason)
        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        summary="Restore a revoked user role",
        description="Restores a previously revoked user role grant, "
        "reinstating the associated permissions immediately. Restoring a "
        "role whose scope (e.g. a project) has been soft-deleted is not "
        "allowed.",
        request=serializers.UserRolePermissionActionSerializer,
        responses={200: OpenApiResponse(description="Role restored successfully.")},
    )
    @action(detail=True, methods=["post"])
    def restore(self, request, uuid=None):
        user_role = self.get_object()
        serializer = serializers.UserRolePermissionActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self._check_action_permission(user_role, get_create_permission)
        validate_scope_not_soft_deleted(user_role.scope)
        if user_role.is_active:
            raise ValidationError("Role is already active.")
        # A revoked grant may have been superseded by a fresh grant of the
        # same role on the same scope (add_user always creates a new row).
        # Restoring the old one would leave two active grants for the same
        # (user, role, scope), so refuse when an equivalent is already active.
        if models.UserRole.objects.filter(
            user=user_role.user,
            role=user_role.role,
            content_type=user_role.content_type,
            object_id=user_role.object_id,
            is_active=True,
        ).exists():
            raise ValidationError("An active grant for this role already exists.")
        reason = (
            serializer.validated_data.get("reason") or "Manual role restore via API"
        )
        user_role.restore(current_user=request.user, reason=reason)
        return Response(status=status.HTTP_200_OK)
