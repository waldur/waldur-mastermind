from django.db import transaction
from rest_framework import serializers

from waldur_core.core import signals as core_signals
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack.utils import _apply_quotas
from waldur_openstack import serializers as openstack_serializers

from .utils import get_external_ip


class MarketplaceTenantCreateSerializer(
    openstack_serializers.OpenStackTenantSerializer
):
    quotas = serializers.JSONField(required=False, default=dict)
    skip_connection_extnet = serializers.BooleanField(default=False)
    mtu = serializers.IntegerField(min_value=68, max_value=9000, required=False)

    class Meta(openstack_serializers.OpenStackTenantSerializer.Meta):
        fields = openstack_serializers.OpenStackTenantSerializer.Meta.fields + (
            "skip_connection_extnet",
            "quotas",
            "mtu",
        )

    @transaction.atomic
    def create(self, validated_data):
        quotas = validated_data.pop("quotas")
        tenant = super().create(validated_data)
        if quotas:
            _apply_quotas(tenant, quotas)
        return tenant

    def _validate_service_settings(self, service_settings, project):
        pass


def get_marketplace_resource_uuid(serializer, volume) -> str | None:
    try:
        resource = marketplace_models.Resource.objects.filter(scope=volume).get()
        return resource.uuid.hex
    except marketplace_models.Resource.DoesNotExist:
        return


def add_marketplace_resource_uuid(sender, fields, **kwargs):
    fields["marketplace_resource_uuid"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_resource_uuid", get_marketplace_resource_uuid)


core_signals.pre_serializer_fields.connect(
    sender=openstack_serializers.OpenStackNestedVolumeSerializer,
    receiver=add_marketplace_resource_uuid,
)


def get_router_external_ips(serializer, router) -> list[str] | None:
    try:
        if not (router.fixed_ips or router.tenant):
            return

        resource = marketplace_models.Resource.objects.filter(scope=router.tenant).get()
        external_ips = []

        for router_fixed_ip in router.fixed_ips:
            external_ip = get_external_ip(resource.offering, router_fixed_ip)
            external_ip and external_ips.append(external_ip)

        return external_ips
    except marketplace_models.Resource.DoesNotExist:
        return


def add_router_external_ips(sender, fields, **kwargs):
    fields["offering_external_ips"] = serializers.SerializerMethodField()
    setattr(sender, "get_offering_external_ips", get_router_external_ips)


core_signals.pre_serializer_fields.connect(
    sender=openstack_serializers.OpenStackRouterSerializer,
    receiver=add_router_external_ips,
)
