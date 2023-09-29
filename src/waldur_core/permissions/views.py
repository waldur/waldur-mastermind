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

from . import models, serializers


def can_destroy_role(role):
    if role.is_system_role:
        raise ValidationError('Destroying of system role is not available.')
    if models.UserRole.objects.filter(is_active=True, role=role).exists():
        raise ValidationError('Role is still used.')


class RoleViewSet(ActionsViewSet):
    queryset = models.Role.objects.all()
    serializer_class = serializers.RoleDetailsSerializer
    lookup_field = 'uuid'
    permission_classes = [IsAdminOrReadOnly]

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


class UserRoleMixin:
    @action(detail=True, methods=['GET'])
    def list_users(self, request, uuid=None):
        scope = self.get_object()
        queryset = get_permissions(scope)
        role = request.query_params.get('role')
        if role:
            if is_uuid_like(role):
                queryset = queryset.filter(role__uuid=role)
            else:
                queryset = queryset.filter(role__name=role)
        queryset = self.paginate_queryset(queryset)
        serializer = serializers.UserRoleDetailsSerializer(queryset, many=True)
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=['POST'])
    def add_user(self, request, uuid=None):
        scope = self.get_object()
        serializer = serializers.UserRoleCreateSerializer(
            data=request.data, context={'scope': scope, 'request': request}
        )
        serializer.is_valid(raise_exception=True)
        target_user = serializer.validated_data['user']
        role = serializer.validated_data['role']
        expiration_time = serializer.validated_data.get('expiration_time')

        perm = add_user(
            scope,
            target_user,
            role,
            created_by=request.user,
            expiration_time=expiration_time,
        )
        return Response(
            status=status.HTTP_201_CREATED,
            data={'expiration_time': perm.expiration_time},
        )

    @action(detail=True, methods=['POST'])
    def update_user(self, request, uuid=None):
        scope = self.get_object()
        serializer = serializers.UserRoleUpdateSerializer(
            data=request.data, context={'scope': scope, 'request': request}
        )
        serializer.is_valid(raise_exception=True)
        target_user = serializer.validated_data['user']
        role = serializer.validated_data['role']
        expiration_time = serializer.validated_data.get('expiration_time')

        perm = update_user(
            scope,
            target_user,
            role,
            expiration_time=expiration_time,
            current_user=request.user,
        )
        return Response(
            status=status.HTTP_200_OK, data={'expiration_time': perm.expiration_time}
        )

    @action(detail=True, methods=['POST'])
    def delete_user(self, request, uuid=None):
        scope = self.get_object()
        serializer = serializers.UserRoleDeleteSerializer(
            data=request.data, context={'scope': scope, 'request': request}
        )
        serializer.is_valid(raise_exception=True)

        target_user = serializer.validated_data['user']
        role = serializer.validated_data['role']

        delete_user(
            scope,
            target_user,
            role,
            request.user,
        )
        return Response(status=status.HTTP_200_OK)
