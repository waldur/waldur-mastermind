from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from waldur_core.core.permissions import IsAdminOrReadOnly
from waldur_core.core.views import ActionsViewSet

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
