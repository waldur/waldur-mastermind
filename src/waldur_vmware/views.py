from __future__ import unicode_literals

from waldur_core.structure import views as structure_views

from . import filters, executors, models, serializers


class ServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.VMwareService.objects.all()
    serializer_class = serializers.ServiceSerializer


class ServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.VMwareServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer
    filter_class = filters.ServiceProjectLinkFilter


class VirtualMachineViewSet(structure_views.BaseResourceViewSet):
    queryset = models.VirtualMachine.objects.all()
    serializer_class = serializers.VirtualMachineSerializer
    filter_class = filters.VirtualMachineFilter
    create_executor = executors.VirtualMachineCreateExecutor
