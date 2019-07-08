from __future__ import unicode_literals

import logging

from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers as rf_serializers, status
from rest_framework.decorators import detail_route
from rest_framework.response import Response

from waldur_core.core import validators as core_validators
from waldur_core.structure import views as structure_views

from . import filters, executors, models, serializers


logger = logging.getLogger(__name__)


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
    pull_executor = executors.VirtualMachinePullExecutor
    create_executor = executors.VirtualMachineCreateExecutor
    delete_executor = executors.VirtualMachineDeleteExecutor
    update_executor = executors.VirtualMachineUpdateExecutor
    update_validators = partial_update_validators = [
        core_validators.StateValidator(models.VirtualMachine.States.OK),
        core_validators.RuntimeStateValidator(models.VirtualMachine.RuntimeStates.POWERED_OFF),
    ]

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

    @detail_route(methods=['post'])
    def create_disk(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        disk = serializer.save()

        transaction.on_commit(lambda: executors.DiskCreateExecutor().execute(disk))
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    create_disk_validators = [
        core_validators.StateValidator(models.VirtualMachine.States.OK),
    ]
    create_disk_serializer_class = serializers.DiskSerializer

    @detail_route(methods=['get'])
    def console(self, request, uuid=None):
        instance = self.get_object()
        backend = instance.get_backend()
        try:
            url = backend.get_console_url(instance)
        except Exception:
            logger.exception('Unable to get console URL.')
            raise rf_serializers.ValidationError('Unable to get console URL.')
        return Response({'url': url}, status=status.HTTP_200_OK)

    console_validators = [core_validators.StateValidator(models.VirtualMachine.States.OK)]


class DiskViewSet(structure_views.BaseResourceViewSet):
    queryset = models.Disk.objects.all()
    serializer_class = serializers.DiskSerializer
    filter_class = filters.DiskFilter
    disabled_actions = ['create', 'update', 'partial_update']
    pull_executor = executors.DiskPullExecutor
    delete_executor = executors.DiskDeleteExecutor

    @detail_route(methods=['post'])
    def extend(self, request, uuid=None):
        """ Increase disk capacity """
        disk = self.get_object()
        serializer = self.get_serializer(disk, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        disk.refresh_from_db()
        transaction.on_commit(lambda: executors.DiskExtendExecutor().execute(disk))

        return Response({'status': _('extend was scheduled')}, status=status.HTTP_202_ACCEPTED)

    extend_validators = [core_validators.StateValidator(models.Disk.States.OK)]
    extend_serializer_class = serializers.DiskExtendSerializer


class TemplateViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Template.objects.all()
    serializer_class = serializers.TemplateSerializer
    filter_class = filters.TemplateFilter
    lookup_field = 'uuid'


class ClusterViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Cluster.objects.all()
    serializer_class = serializers.ClusterSerializer
    filter_class = filters.ClusterFilter
    lookup_field = 'uuid'


class NetworkViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Network.objects.all()
    serializer_class = serializers.NetworkSerializer
    filter_class = filters.NetworkFilter
    lookup_field = 'uuid'


class DatastoreViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Datastore.objects.all()
    serializer_class = serializers.DatastoreSerializer
    filter_class = filters.DatastoreFilter
    lookup_field = 'uuid'
