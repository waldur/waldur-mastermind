from rest_framework import serializers

from . import models


class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Role
        fields = ('name', 'description', 'permissions')

    permissions = serializers.SerializerMethodField()

    def get_permissions(self, role):
        return models.RolePermission.objects.filter(role=role).values_list(
            'permission', flat=True
        )
