from django.http import HttpResponse
from django.utils.translation import ugettext_lazy as _
from rest_framework import decorators, exceptions, viewsets, response, status, serializers as rf_serializers

from waldur_core.core import validators as core_validators
from waldur_core.structure import views as structure_views

from . import models, serializers, executors, filters
from .backend import SizeQueryset


class AzureServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.AzureService.objects.all()
    serializer_class = serializers.ServiceSerializer
    import_serializer_class = serializers.VirtualMachineImportSerializer


class AzureServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.AzureServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer


class ImageViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Image.objects.all()
    serializer_class = serializers.ImageSerializer
    filter_class = filters.ImageFilter
    lookup_field = 'uuid'

    def get_queryset(self):
        return models.Image.objects.order_by('name')


class SizeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SizeQueryset()
    serializer_class = serializers.SizeSerializer
    lookup_field = 'uuid'


class VirtualMachineViewSet(structure_views.BaseResourceViewSet):
    queryset = models.VirtualMachine.objects.all()
    filter_class = filters.VirtualMachineFilter
    serializer_class = serializers.VirtualMachineSerializer
    delete_executor = executors.VirtualMachineDeleteExecutor

    @decorators.detail_route()
    def rdp(self, request, uuid=None):
        vm = self.get_object()

        try:
            rdp_endpoint = vm.endpoints.get(name=models.InstanceEndpoint.Name.RDP)
        except models.InstanceEndpoint.DoesNotExist:
            raise exceptions.NotFound("This virtual machine doesn't run remote desktop")

        response = HttpResponse(content_type='application/x-rdp')
        response['Content-Disposition'] = 'attachment; filename="{}.rdp"'.format(vm.name)
        response.write(
            "full address:s:%s.cloudapp.net:%s\n"
            "prompt for credentials:i:1\n\n" % (vm.service_project_link.cloud_service_name, rdp_endpoint.public_port))

        return response

    rdp_validators = [core_validators.StateValidator(models.VirtualMachine.States.OK),
                      core_validators.RuntimeStateValidator('running')]

    @decorators.detail_route(methods=['post'])
    def start(self, request, uuid=None):
        virtual_machine = self.get_object()
        executors.VirtualMachineStartExecutor().execute(virtual_machine)
        return response.Response({'status': _('start was scheduled')}, status=status.HTTP_202_ACCEPTED)

    start_validators = [core_validators.StateValidator(models.VirtualMachine.States.OK),
                        core_validators.RuntimeStateValidator('stopped')]
    start_serializer_class = rf_serializers.Serializer

    @decorators.detail_route(methods=['post'])
    def stop(self, request, uuid=None):
        virtual_machine = self.get_object()
        executors.VirtualMachineStopExecutor().execute(virtual_machine)
        return response.Response({'status': _('stop was scheduled')}, status=status.HTTP_202_ACCEPTED)

    stop_validators = [core_validators.StateValidator(models.VirtualMachine.States.OK),
                       core_validators.RuntimeStateValidator('running')]
    stop_serializer_class = rf_serializers.Serializer

    @decorators.detail_route(methods=['post'])
    def restart(self, request, uuid=None):
        virtual_machine = self.get_object()
        executors.VirtualMachineRestartExecutor().execute(virtual_machine)
        return response.Response({'status': _('restart was scheduled')}, status=status.HTTP_202_ACCEPTED)

    restart_validators = [core_validators.StateValidator(models.VirtualMachine.States.OK),
                          core_validators.RuntimeStateValidator('running')]
    restart_serializer_class = rf_serializers.Serializer

    def perform_create(self, serializer):
        instance = serializer.save()
        executors.VirtualMachineCreateExecutor.execute(
            instance,
            backend_image_id=serializer.validated_data['image'].backend_id,
            backend_size_id=serializer.validated_data['size'].pk,
        )
