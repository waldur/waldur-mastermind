from rest_framework import serializers

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
