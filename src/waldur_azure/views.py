from django.db import transaction
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from rest_framework import decorators, response, status, viewsets

from waldur_core.core import validators as core_validators
from waldur_core.core.enums import CoreStates
from waldur_core.core.serializers import StatusSerializer
from waldur_core.structure import views as structure_views

from . import executors, filters, models, serializers


class ImageViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Image.objects.all()
    serializer_class = serializers.AzureImageSerializer
    filterset_class = filters.ImageFilter
    lookup_field = "uuid"

    def get_queryset(self):
        return models.Image.objects.order_by("name")


class SizeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.Size.objects.all()
    serializer_class = serializers.AzureSizeSerializer
    filterset_class = filters.SizeFilter
    lookup_field = "uuid"


class LocationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.Location.objects.filter(enabled=True)
    serializer_class = serializers.AzureLocationSerializer
    filterset_class = filters.LocationFilter
    lookup_field = "uuid"


class ResourceGroupViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.ResourceGroup.objects.all().order_by("name")
    serializer_class = serializers.AzureResourceGroupSerializer
    lookup_field = "uuid"


class PublicIPViewSet(structure_views.ResourceViewSet):
    queryset = models.PublicIP.objects.all().order_by("name")
    filterset_class = filters.PublicIPFilter
    serializer_class = serializers.AzurePublicIPSerializer
    create_executor = executors.PublicIPCreateExecutor
    delete_executor = executors.PublicIPDeleteExecutor


class VirtualMachineViewSet(
    structure_views.ResourceViewSet, structure_views.AvailabilityCheckViewMixin
):
    queryset = models.VirtualMachine.objects.all().order_by("name")
    filterset_class = filters.VirtualMachineFilter
    serializer_class = serializers.AzureVirtualMachineSerializer
    create_executor = executors.VirtualMachineCreateExecutor
    delete_executor = executors.VirtualMachineDeleteExecutor
    pull_executor = executors.VirtualMachinePullExecutor

    @extend_schema(request=None, responses={status.HTTP_202_ACCEPTED: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def start(self, request, uuid=None):
        virtual_machine: models.VirtualMachine = self.get_object()
        executors.VirtualMachineStartExecutor().execute(virtual_machine)
        return response.Response(
            {"status": _("start was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    start_validators = [
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator("stopped"),
    ]

    @extend_schema(request=None, responses={status.HTTP_202_ACCEPTED: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def stop(self, request, uuid=None):
        virtual_machine: models.VirtualMachine = self.get_object()
        executors.VirtualMachineStopExecutor().execute(virtual_machine)
        return response.Response(
            {"status": _("stop was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    stop_validators = [
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator("running"),
    ]

    @extend_schema(request=None, responses={status.HTTP_202_ACCEPTED: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def restart(self, request, uuid=None):
        virtual_machine: models.VirtualMachine = self.get_object()
        executors.VirtualMachineRestartExecutor().execute(virtual_machine)
        return response.Response(
            {"status": _("restart was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    restart_validators = [
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator("running"),
    ]


class SQLServerViewSet(
    structure_views.ResourceViewSet, structure_views.AvailabilityCheckViewMixin
):
    queryset = models.SQLServer.objects.all().order_by("name")
    filterset_class = filters.SQLServerFilter
    serializer_class = serializers.AzureSqlServerSerializer
    create_executor = executors.SQLServerCreateExecutor
    delete_executor = executors.SQLServerDeleteExecutor

    @extend_schema(
        responses={
            status.HTTP_202_ACCEPTED: serializers.AzureSqlDatabaseCreateResponseSerializer
        }
    )
    @decorators.action(detail=True, methods=["post"])
    def create_database(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        database = serializer.save()

        transaction.on_commit(
            lambda: executors.SQLDatabaseCreateExecutor().execute(database)
        )

        payload = {
            "status": _("SQL database creation was scheduled"),
            "database_uuid": database.uuid.hex,
        }
        return response.Response(payload, status=status.HTTP_202_ACCEPTED)

    create_database_validators = [core_validators.StateValidator(CoreStates.OK)]
    create_database_serializer_class = serializers.AzureSqlDatabaseCreateSerializer


class SQLDatabaseViewSet(structure_views.ResourceViewSet):
    queryset = models.SQLDatabase.objects.all().order_by("name")
    filterset_class = filters.SQLDatabaseFilter
    serializer_class = serializers.AzureSqlDatabaseSerializer
    create_executor = executors.SQLDatabaseCreateExecutor
    delete_executor = executors.SQLDatabaseDeleteExecutor
