from django.db import transaction
from rest_framework import serializers

from waldur_mastermind.marketplace_openstack.utils import _apply_quotas
from waldur_openstack.openstack import serializers as openstack_serializers


class MarketplaceTenantCreateSerializer(openstack_serializers.TenantSerializer):
    quotas = serializers.JSONField(required=False, default=dict)
    skip_connection_extnet = serializers.BooleanField(default=False)

    class Meta(openstack_serializers.TenantSerializer.Meta):
        fields = openstack_serializers.TenantSerializer.Meta.fields + (
            'skip_connection_extnet',
            'quotas',
        )

    def _validate_service_project_link(self, spl):
        # We shall skip permission check when marketplace order item is being created
        pass

    @transaction.atomic
    def create(self, validated_data):
        quotas = validated_data.pop('quotas')
        tenant = super(MarketplaceTenantCreateSerializer, self).create(validated_data)
        if quotas:
            _apply_quotas(tenant, quotas)
        return tenant
