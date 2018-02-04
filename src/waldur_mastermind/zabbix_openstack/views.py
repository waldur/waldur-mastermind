from waldur_zabbix import views as zabbix_views

from . import filters, serializers


class LinkViewSet(zabbix_views.ZabbixServiceProjectLinkViewSet):
    serializer_class = serializers.LinkSerializer
    filter_backends = zabbix_views.ZabbixServiceProjectLinkViewSet.filter_backends + (
        filters.LinkFilterBackend,
    )
