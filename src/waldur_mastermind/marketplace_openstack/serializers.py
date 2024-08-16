import ipaddress

from django.db import transaction
from rest_framework import serializers

from waldur_core.core import signals as core_signals
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack.utils import _apply_quotas
from waldur_openstack.openstack import serializers as openstack_serializers
from waldur_openstack.openstack_tenant import (
    serializers as openstack_tenant_serializers,
)


class MarketplaceTenantCreateSerializer(openstack_serializers.TenantSerializer):
    quotas = serializers.JSONField(required=False, default=dict)
    skip_connection_extnet = serializers.BooleanField(default=False)

    class Meta(openstack_serializers.TenantSerializer.Meta):
        fields = openstack_serializers.TenantSerializer.Meta.fields + (
            "skip_connection_extnet",
            "quotas",
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


def get_marketplace_resource_uuid(serializer, volume):
    try:
        resource = marketplace_models.Resource.objects.filter(scope=volume).get()
        return resource.uuid.hex
    except marketplace_models.Resource.DoesNotExist:
        return


def add_marketplace_resource_uuid(sender, fields, **kwargs):
    fields["marketplace_resource_uuid"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_resource_uuid", get_marketplace_resource_uuid)


core_signals.pre_serializer_fields.connect(
    sender=openstack_tenant_serializers.NestedVolumeSerializer,
    receiver=add_marketplace_resource_uuid,
)


def _get_external_ips(offering, ips):
    external_ips = []
    ipv4_external_ip_mapping = offering.secret_options.get(
        "ipv4_external_ip_mapping", []
    )
    if not (ipv4_external_ip_mapping or ips or offering):
        return

    for ip in ips:
        ip_address = ipaddress.ip_address(ip)

        for offering_external_ip in ipv4_external_ip_mapping:
            ip_network = ipaddress.ip_network(offering_external_ip["floating_ip"])

            if ip_address in ip_network:
                external_ips.append(
                    ".".join(offering_external_ip["external_ip"].split(".")[:-1])
                    + "."
                    + ip.split(".")[-1]
                )

    return external_ips


def get_instance_external_ips(serializer, instance):
    try:
        if not instance.floating_ips.exists():
            return

        floating_ips = instance.floating_ips.all()
        resource = marketplace_models.Resource.objects.filter(scope=instance).get()
        return _get_external_ips(
            resource.offering.parent, [i.address for i in floating_ips]
        )
    except marketplace_models.Resource.DoesNotExist:
        return


def add_resource_external_ips(sender, fields, **kwargs):
    fields["offering_external_ips"] = serializers.SerializerMethodField()
    setattr(sender, "get_offering_external_ips", get_instance_external_ips)


core_signals.pre_serializer_fields.connect(
    sender=openstack_tenant_serializers.InstanceSerializer,
    receiver=add_resource_external_ips,
)


def get_router_external_ips(serializer, router):
    try:
        if not (router.fixed_ips or router.tenant):
            return

        resource = marketplace_models.Resource.objects.filter(scope=router.tenant).get()
        return _get_external_ips(resource.offering, router.fixed_ips)
    except marketplace_models.Resource.DoesNotExist:
        return


def add_router_external_ips(sender, fields, **kwargs):
    fields["offering_external_ips"] = serializers.SerializerMethodField()
    setattr(sender, "get_offering_external_ips", get_router_external_ips)


core_signals.pre_serializer_fields.connect(
    sender=openstack_serializers.RouterSerializer,
    receiver=add_router_external_ips,
)
