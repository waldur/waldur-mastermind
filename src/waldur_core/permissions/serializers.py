from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from waldur_core.permissions.enums import PermissionEnum

from . import models


class RoleDetailsSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Role
        fields = (
            'uuid',
            'name',
            'description',
            'permissions',
            'is_system_role',
            'users_count',
        )
        extra_kwargs = {'is_system_role': {'read_only': True}}

    permissions = serializers.SerializerMethodField()
    users_count = serializers.SerializerMethodField()

    def get_permissions(self, role):
        return list(
            models.RolePermission.objects.filter(role=role).values_list(
                'permission', flat=True
            )
        )

    def get_users_count(self, role):
        return models.UserRole.objects.filter(is_active=True, role=role).count()


class RoleModifySerializer(RoleDetailsSerializer):
    permissions = serializers.JSONField()

    def validate(self, attrs):
        if self.instance and self.instance.is_system_role:
            if attrs.get('name') != self.instance.name:
                raise ValidationError('Changing name for system role is not possible.')
        return attrs

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
