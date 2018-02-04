from rest_framework import serializers

from waldur_core.core import signals as core_signals
from waldur_openstack.openstack_tenant import serializers as openstack_serializers
from waldur_zabbix import models as zabbix_models
from waldur_zabbix import serializers as zabbix_serializers


class LinkSerializer(zabbix_serializers.ServiceProjectLinkSerializer):
    internal_ip = serializers.SerializerMethodField()
    service_settings = serializers.HyperlinkedRelatedField(
        source='service.settings',
        view_name='servicesettings-detail',
        read_only=True,
        lookup_field='uuid')
    service_settings_uuid = serializers.ReadOnlyField(source='service.settings.uuid')

    def get_internal_ip(self, link):
        scope = link.service.settings.scope
        if not scope:
            return
        if not hasattr(scope, 'internal_ips'):
            return
        # Note that we do not support multiple IPs per VM yet
        if isinstance(scope.internal_ips, list):
            return scope.internal_ips[0]
        return scope.internal_ips

    class Meta(zabbix_serializers.ServiceProjectLinkSerializer.Meta):
        fields = zabbix_serializers.ServiceProjectLinkSerializer.Meta.fields + (
            'internal_ip', 'service_settings', 'service_settings_uuid',
        )


class NestedHostSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField(source='get_state_display')

    class Meta(serializers.HyperlinkedModelSerializer):
        model = zabbix_models.Host
        fields = ('url', 'uuid', 'state')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'zabbix-host-detail'},
        }


def get_zabbix_host(serializer, scope):
    host = zabbix_models.Host.objects.filter(scope=scope).last()
    if host:
        serializer = NestedHostSerializer(instance=host, context=serializer.context)
        return serializer.data


def add_zabbix_host(sender, fields, **kwargs):
    fields['zabbix_host'] = serializers.SerializerMethodField()
    setattr(sender, 'get_zabbix_host', get_zabbix_host)


core_signals.pre_serializer_fields.connect(
    sender=openstack_serializers.InstanceSerializer,
    receiver=add_zabbix_host,
)
