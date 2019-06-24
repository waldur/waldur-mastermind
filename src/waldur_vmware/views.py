from __future__ import unicode_literals

from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers as rf_serializers, status
from rest_framework.decorators import detail_route
from rest_framework.response import Response

from waldur_core.core import validators as core_validators
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
    delete_executor = executors.VirtualMachineDeleteExecutor

    @detail_route(methods=['post'])
    def start(self, request, uuid=None):
        instance = self.get_object()
        executors.VirtualMachineStartExecutor().execute(instance)
        return Response({'status': _('start was scheduled')}, status=status.HTTP_202_ACCEPTED)

    start_validators = [
        core_validators.StateValidator(models.VirtualMachine.States.OK),
        core_validators.RuntimeStateValidator(
            models.VirtualMachine.RuntimeStates.POWERED_OFF,
            models.VirtualMachine.RuntimeStates.SUSPENDED,
        ),
    ]
    start_serializer_class = rf_serializers.Serializer

    @detail_route(methods=['post'])
    def stop(self, request, uuid=None):
        instance = self.get_object()
        executors.VirtualMachineStopExecutor().execute(instance)
        return Response({'status': _('stop was scheduled')}, status=status.HTTP_202_ACCEPTED)

    stop_validators = [
        core_validators.StateValidator(models.VirtualMachine.States.OK),
        core_validators.RuntimeStateValidator(
            models.VirtualMachine.RuntimeStates.POWERED_ON,
            models.VirtualMachine.RuntimeStates.SUSPENDED,
        ),
    ]
    stop_serializer_class = rf_serializers.Serializer

    @detail_route(methods=['post'])
    def reset(self, request, uuid=None):
        instance = self.get_object()
        executors.VirtualMachineResetExecutor().execute(instance)
        return Response({'status': _('reset was scheduled')}, status=status.HTTP_202_ACCEPTED)

    reset_validators = [
        core_validators.StateValidator(models.VirtualMachine.States.OK),
        core_validators.RuntimeStateValidator(
            models.VirtualMachine.RuntimeStates.POWERED_ON,
        ),
    ]
    reset_serializer_class = rf_serializers.Serializer

    @detail_route(methods=['post'])
    def suspend(self, request, uuid=None):
        instance = self.get_object()
        executors.VirtualMachineSuspendExecutor().execute(instance)
        return Response({'status': _('suspend was scheduled')}, status=status.HTTP_202_ACCEPTED)

    suspend_validators = [
        core_validators.StateValidator(models.VirtualMachine.States.OK),
        core_validators.RuntimeStateValidator(
            models.VirtualMachine.RuntimeStates.POWERED_ON,
        ),
    ]
    suspend_serializer_class = rf_serializers.Serializer
