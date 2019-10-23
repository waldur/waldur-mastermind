import logging

from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers as rf_serializers, status
from rest_framework.decorators import action
from rest_framework.mixins import RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from waldur_core.core import validators as core_validators
from waldur_core.structure import models as structure_models
from waldur_core.structure import views as structure_views
from waldur_vmware.apps import VMwareConfig

from . import filters, executors, models, serializers


logger = logging.getLogger(__name__)


class ServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.VMwareService.objects.all()
    serializer_class = serializers.ServiceSerializer


class ServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.VMwareServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer
    filterset_class = filters.ServiceProjectLinkFilter


class LimitViewSet(RetrieveModelMixin, GenericViewSet):
    """
    Service consumer is not allowed to get details of service settings of service provider.
    However, currently VMware virtual machine limits are stored as options in service settings.
    Therefore in order to implement frontent-side validation of VM configuration in deployment form,
    we need to get limits of VMware service settings for service consumer.
    That's why GenericRoleFilter is not applied here.
    It is expected that eventually service provider limits would be moved to marketplace offering.
    """
    queryset = structure_models.ServiceSettings.objects.filter(type=VMwareConfig.service_name)
    lookup_field = 'uuid'
    serializer_class = serializers.LimitSerializer


class VirtualMachineViewSet(structure_views.BaseResourceViewSet):
    queryset = models.VirtualMachine.objects.all()
    serializer_class = serializers.VirtualMachineSerializer
    filterset_class = filters.VirtualMachineFilter
    pull_executor = executors.VirtualMachinePullExecutor
    create_executor = executors.VirtualMachineCreateExecutor
    delete_executor = executors.VirtualMachineDeleteExecutor
    update_executor = executors.VirtualMachineUpdateExecutor
    update_validators = partial_update_validators = [
        core_validators.StateValidator(models.VirtualMachine.States.OK),
        core_validators.RuntimeStateValidator(models.VirtualMachine.RuntimeStates.POWERED_OFF),
    ]

    destroy_validators = structure_views.BaseResourceViewSet.destroy_validators + [
        core_validators.RuntimeStateValidator(models.VirtualMachine.RuntimeStates.POWERED_OFF)
    ]

    @action(detail=True, methods=['post'])
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

    @action(detail=True, methods=['post'])
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

    @action(detail=True, methods=['post'])
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

    @action(detail=True, methods=['post'])
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

    def vm_tools_are_running(vm):
        if vm.tools_state != models.VirtualMachine.ToolsStates.RUNNING:
            raise rf_serializers.ValidationError('VMware Tools are not running.')

    @action(detail=True, methods=['post'])
    def shutdown_guest(self, request, uuid=None):
        instance = self.get_object()
        executors.VirtualMachineShutdownGuestExecutor().execute(instance)
        return Response({'status': _('shutdown was scheduled')}, status=status.HTTP_202_ACCEPTED)

    shutdown_guest_validators = reboot_guest_validators = [
        core_validators.StateValidator(models.VirtualMachine.States.OK),
        core_validators.RuntimeStateValidator(
            models.VirtualMachine.RuntimeStates.POWERED_ON,
        ),
        vm_tools_are_running,
    ]
    shutdown_guest_serializer_class = rf_serializers.Serializer

    @action(detail=True, methods=['post'])
    def reboot_guest(self, request, uuid=None):
        instance = self.get_object()
        executors.VirtualMachineRebootGuestExecutor().execute(instance)
        return Response({'status': _('reboot was scheduled')}, status=status.HTTP_202_ACCEPTED)

    reboot_guest_serializer_class = rf_serializers.Serializer

    @action(detail=True, methods=['post'])
    def create_port(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        port = serializer.save()

        transaction.on_commit(lambda: executors.PortCreateExecutor().execute(port))
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def check_number_of_ports(vm):
        # Limit of the network adapter per VM is 10 in vSphere 6.7, 6.5 and 6.0
        if vm.port_set.count() >= 10:
            raise rf_serializers.ValidationError('Virtual machine can have at most 10 network adapters.')

    create_port_validators = [
        core_validators.StateValidator(models.VirtualMachine.States.OK),
        check_number_of_ports,
    ]
    create_port_serializer_class = serializers.PortSerializer

    @action(detail=True, methods=['post'])
    def create_disk(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        disk = serializer.save()

        transaction.on_commit(lambda: executors.DiskCreateExecutor().execute(disk))
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def validate_total_size(vm):
        max_disk_total = serializers.get_int_or_none(vm.service_settings.options, 'max_disk_total')

        if max_disk_total:
            remaining_quota = max_disk_total - vm.total_disk
            if remaining_quota < 1024:
                raise rf_serializers.ValidationError('Storage quota has been reached.')

    create_disk_validators = [
        core_validators.StateValidator(models.VirtualMachine.States.OK),
        validate_total_size,
    ]
    create_disk_serializer_class = serializers.DiskSerializer

    @action(detail=True, methods=['get'])
    def console(self, request, uuid=None):
        """
        This endpoint provides access to Virtual Machine Remote Console aka VMRC.
        """
        instance = self.get_object()
        backend = instance.get_backend()
        try:
            url = backend.get_console_url(instance)
        except Exception:
            logger.exception('Unable to get console URL.')
            raise rf_serializers.ValidationError('Unable to get console URL.')
        return Response({'url': url}, status=status.HTTP_200_OK)

    console_validators = [core_validators.StateValidator(models.VirtualMachine.States.OK)]

    @action(detail=True, methods=['get'])
    def web_console(self, request, uuid=None):
        """
        This endpoint provides access to HTML Console aka WMKS.
        """
        instance = self.get_object()
        backend = instance.get_backend()
        try:
            url = backend.get_web_console_url(instance)
        except Exception:
            logger.exception('Unable to get web console URL.')
            raise rf_serializers.ValidationError('Unable to get web console URL.')
        return Response({'url': url}, status=status.HTTP_200_OK)

    web_console_validators = [
        core_validators.StateValidator(models.VirtualMachine.States.OK),
        core_validators.RuntimeStateValidator(models.VirtualMachine.RuntimeStates.POWERED_ON)
    ]


class PortViewSet(structure_views.BaseResourceViewSet):
    queryset = models.Port.objects.all().order_by('-created')
    serializer_class = serializers.PortSerializer
    filterset_class = filters.PortFilter
    disabled_actions = ['create', 'update', 'partial_update']
    pull_executor = executors.PortPullExecutor
    delete_executor = executors.PortDeleteExecutor


class DiskViewSet(structure_views.BaseResourceViewSet):
    queryset = models.Disk.objects.all().order_by('-created')
    serializer_class = serializers.DiskSerializer
    filterset_class = filters.DiskFilter
    disabled_actions = ['create', 'update', 'partial_update']
    pull_executor = executors.DiskPullExecutor
    delete_executor = executors.DiskDeleteExecutor

    @action(detail=True, methods=['post'])
    def extend(self, request, uuid=None):
        """ Increase disk capacity """
        disk = self.get_object()
        serializer = self.get_serializer(disk, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        disk.refresh_from_db()
        transaction.on_commit(lambda: executors.DiskExtendExecutor().execute(disk))

        return Response({'status': _('extend was scheduled')}, status=status.HTTP_202_ACCEPTED)

    def validate_total_size(disk):
        options = disk.vm.service_settings.options

        max_disk = serializers.get_int_or_none(options, 'max_disk')
        if max_disk and abs(max_disk - disk.size) < 1024:
            raise rf_serializers.ValidationError('Storage limit has been reached.')

        max_disk_total = serializers.get_int_or_none(options, 'max_disk_total')

        if max_disk_total:
            remaining_quota = max_disk_total - disk.vm.total_disk
            if remaining_quota < 1024:
                raise rf_serializers.ValidationError('Storage quota has been reached.')

    extend_validators = [core_validators.StateValidator(models.Disk.States.OK), validate_total_size]
    extend_serializer_class = serializers.DiskExtendSerializer


class TemplateViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Template.objects.all()
    serializer_class = serializers.TemplateSerializer
    filterset_class = filters.TemplateFilter
    lookup_field = 'uuid'


class ClusterViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Cluster.objects.all()
    serializer_class = serializers.ClusterSerializer
    filterset_class = filters.ClusterFilter
    lookup_field = 'uuid'


class NetworkViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Network.objects.all()
    serializer_class = serializers.NetworkSerializer
    filterset_class = filters.NetworkFilter
    lookup_field = 'uuid'


class DatastoreViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Datastore.objects.all()
    serializer_class = serializers.DatastoreSerializer
    filterset_class = filters.DatastoreFilter
    lookup_field = 'uuid'


class FolderViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Folder.objects.all()
    serializer_class = serializers.FolderSerializer
    filterset_class = filters.FolderFilter
    lookup_field = 'uuid'
