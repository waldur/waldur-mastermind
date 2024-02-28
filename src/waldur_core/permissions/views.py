import logging

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from waldur_core.core.permissions import IsAdminOrReadOnly
from waldur_core.core.utils import is_uuid_like
from waldur_core.core.views import ActionsViewSet
from waldur_core.permissions.utils import (
    add_user,
    delete_user,
    get_permissions,
    update_user,
)

from . import filters, models, serializers

User = get_user_model()
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

    def create(self, request):
        serializer = serializers.RoleModifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role = serializer.save()
        serializer = serializers.RoleDetailsSerializer(instance=role)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def update(self, request, **kwargs):
        instance = self.get_object()
        serializer = serializers.RoleModifySerializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        role = serializer.save()
        serializer = serializers.RoleDetailsSerializer(instance=role)
        return Response(serializer.data, status=status.HTTP_200_OK)

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
        role = request.query_params.get("role")
        search_string = request.query_params.get("search_string")
        if search_string:
            queryset = queryset.filter(
                Q(user__first_name__icontains=search_string)
                | Q(user__last_name__icontains=search_string)
                | Q(user__email__icontains=search_string)
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
