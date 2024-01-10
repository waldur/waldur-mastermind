from rest_framework import serializers

from waldur_core.structure import serializers as structure_serializers

from . import models


class TestServiceSerializer(structure_serializers.ServiceOptionsSerializer):
    class Meta:
        secret_fields = ("backend_url", "username", "password")

    backend_url = serializers.CharField(
        max_length=200,
    )

    username = serializers.CharField(max_length=100)

    password = serializers.CharField(max_length=100)

    tenant_name = serializers.CharField(
        source="options.tenant_name",
        default="admin",
        required=False,
    )

    availability_zone = serializers.CharField(
        source="options.availability_zone",
        required=False,
    )


class NewInstanceSerializer(structure_serializers.VirtualMachineSerializer):
    class Meta(structure_serializers.VirtualMachineSerializer.Meta):
        model = models.TestNewInstance
