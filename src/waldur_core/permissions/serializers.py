from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from waldur_core.permissions.enums import PermissionEnum

from . import models


class RoleDetailsSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Role
        fields = ('uuid', 'name', 'description', 'permissions')

    permissions = serializers.SerializerMethodField()

    def get_permissions(self, role):
        return list(
            models.RolePermission.objects.filter(role=role).values_list(
                'permission', flat=True
            )
        )


class RoleModifySerializer(RoleDetailsSerializer):
    permissions = serializers.JSONField()

    def validate_permissions(self, permissions):
        invalid = set(permissions) - set(perm.value for perm in PermissionEnum)
        if invalid:
            raise ValidationError(f'Invalid permissions {",".join(invalid)}')
        return permissions

    def create(self, validated_data):
        permissions = validated_data.pop('permissions')
        role = super().create(validated_data)
        for permission in permissions:
            models.RolePermission.objects.create(role=role, permission=permission)
        return role

    def update(self, instance, validated_data):
        current_permissions = set(
            models.RolePermission.objects.filter(role=instance).values_list(
                'permission', flat=True
            )
        )
        new_permissions = set(validated_data.pop('permissions'))
        role = super().update(instance, validated_data)
        models.RolePermission.objects.filter(
            role=role, permission__in=current_permissions - new_permissions
        ).delete()

        for permission in new_permissions - current_permissions:
            models.RolePermission.objects.create(role=role, permission=permission)

        return role
