import logging
import uuid

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _
from django_filters import utils as django_filters_utils
from django_filters.rest_framework.backends import DjangoFilterBackend
from drf_spectacular.plumbing import (
    OpenApiTypes,
    build_array_type,
    build_basic_type,
)
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from waldur_core.core.models import User
from waldur_core.core.permissions import IsAdminOrReadOnly
from waldur_core.core.utils import get_ip_address, is_uuid_like
from waldur_core.core.views import ActionsViewSet
from waldur_core.permissions.filters import UserPermissionFilter
from waldur_core.permissions.utils import (
    add_user,
    delete_user,
    get_permissions,
    update_user,
)

from . import filters, models, serializers

logger = logging.getLogger(__name__)


def can_destroy_role(role):
    if role.is_system_role:
        raise ValidationError("Destroying of system role is not available.")
    if models.UserRole.objects.filter(is_active=True, role=role).exists():
        raise ValidationError("Role is still used.")


class RoleViewSet(ActionsViewSet):
    queryset = models.Role.objects.all()
    serializer_class = serializers.RoleDetailsSerializer
    lookup_field = "uuid"
    permission_classes = [IsAdminOrReadOnly]
    filterset_class = filters.RoleFilter
    destroy_validators = [can_destroy_role]

    @extend_schema(
        request=serializers.RoleModifySerializer,
        responses=serializers.RoleDetailsSerializer,
    )
    def create(self, request):
        serializer = serializers.RoleModifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role: models.Role = serializer.save()
        serializer = serializers.RoleDetailsSerializer(instance=role)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=serializers.RoleModifySerializer,
        responses=serializers.RoleDetailsSerializer,
    )
    def update(self, request, **kwargs):
        instance: models.Role = self.get_object()
        serializer = serializers.RoleModifySerializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        role: models.Role = serializer.save()
        serializer = serializers.RoleDetailsSerializer(instance=role)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        request=serializers.RoleDescriptionSerializer,
        responses=serializers.RoleDescriptionSerializer,
    )
    @action(detail=True, methods=["PUT"])
    def update_descriptions(self, request, uuid=None):
        instance: models.Role = self.get_object()
        serializer = serializers.RoleDescriptionSerializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        request=None,
        responses=None,
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
        request=None,
        responses=None,
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


class UserRoleMixin:
    """Mixin to provide user role management functionality for viewsets."""

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="user",
                type=uuid.UUID,
                location=OpenApiParameter.QUERY,
                description="User UUID",
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
                type=uuid.UUID,
                location=OpenApiParameter.QUERY,
                description="Role UUID or name",
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
    )
    @action(detail=True, methods=["GET"])
    def list_users(self, request, uuid=None):
        scope = self.get_object()
        user_uuid = request.query_params.get("user")

        user = None
        if user_uuid and is_uuid_like(user_uuid):
            try:
                user = User.objects.get(uuid=user_uuid)
            except User.DoesNotExist:
                pass
        queryset = get_permissions(scope, user)

        kwargs = DjangoFilterBackend().get_filterset_kwargs(request, queryset, self)
        filterset = UserPermissionFilter(**kwargs)

        if not filterset.is_valid():
            raise django_filters_utils.translate_validation(filterset.errors)

        queryset = filterset.qs

        role = request.query_params.get("role")
        search_string = request.query_params.get("search_string")
        if search_string:
            queryset = queryset.filter(
                Q(user__first_name__icontains=search_string)
                | Q(user__last_name__icontains=search_string)
                | Q(user__email__icontains=search_string)
                | Q(user__username__icontains=search_string)
            ).distinct()
        if role:
            if is_uuid_like(role):
                queryset = queryset.filter(role__uuid=role)
            else:
                queryset = queryset.filter(role__name=role)
        queryset = self.paginate_queryset(queryset)
        serializer = serializers.UserRoleDetailsSerializer(
            queryset, many=True, context={"request": request}
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        request=serializers.UserRoleCreateSerializer,
        responses={201: serializers.UserRoleExpirationTimeSerializer},
    )
    @action(detail=True, methods=["POST"])
    def add_user(self, request, uuid=None):
        scope = self.get_object()
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
        request=serializers.UserRoleUpdateSerializer,
        responses={200: serializers.UserRoleExpirationTimeSerializer},
    )
    @action(detail=True, methods=["POST"])
    def update_user(self, request, uuid=None):
        scope = self.get_object()
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
        request=serializers.UserRoleDeleteSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["POST"])
    def delete_user(self, request, uuid=None):
        scope = self.get_object()
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


class UserPermissionViewSet(ReadOnlyModelViewSet):
    queryset = models.UserRole.objects.filter(is_active=True)
    serializer_class = serializers.PermissionSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.UserPermissionFilter

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        qs = qs.filter(user=user).distinct()
        user_ip = get_ip_address(self.request)
        if not user_ip:
            return qs
        return filter_user_permissions_by_ip_address(qs, user_ip)
