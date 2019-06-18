from __future__ import unicode_literals

from waldur_core.structure import views as structure_views

from . import filters, models, serializers


class ServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.VMwareService.objects.all()
    serializer_class = serializers.ServiceSerializer


class ServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.VMwareServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer
    filter_class = filters.ServiceProjectLinkFilter
