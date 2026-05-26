from rest_framework import status
from drf_spectacular.utils import extend_schema
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import decorators, response, status

from waldur_core.core import executors as core_executors
from waldur_core.core import validators as core_validators
from waldur_core.core.enums import CoreStates
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure import views as structure_views
from waldur_core.core.serializers import StatusSerializer

from . import executors, filters, models, serializers


class ImageViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Image.objects.all().order_by("name")
    serializer_class = serializers.DigitalOceanImageSerializer
    filterset_class = filters.ImageFilter
    lookup_field = "uuid"


class RegionViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Region.objects.all()
    serializer_class = serializers.DigitalOceanRegionSerializer
    filterset_class = filters.RegionFilter
    lookup_field = "uuid"

    def get_queryset(self):
        return models.Region.objects.order_by("name")


class SizeViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Size.objects.all().order_by("name")
    serializer_class = serializers.DigitalOceanSizeSerializer
    filterset_class = filters.SizeFilter
    lookup_field = "uuid"


class DropletViewSet(structure_views.ResourceViewSet):
    queryset = models.Droplet.objects.all().order_by("name")
    serializer_class = serializers.DigitalOceanDropletSerializer
    filterset_class = filters.DropletFilter
    create_executor = executors.DropletCreateExecutor
    update_executor = core_executors.EmptyExecutor
    delete_executor = executors.DropletDeleteExecutor
    destroy_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED)
    ]

    def perform_create(self, serializer):
        region = serializer.validated_data["region"]
        image = serializer.validated_data["image"]
        size = serializer.validated_data["size"]
        ssh_key = serializer.validated_data.get("ssh_public_key")

        droplet: models.Droplet = serializer.save(
            cores=size.cores, ram=size.ram, disk=size.disk, transfer=size.transfer
        )

        # XXX: We do not operate with backend_id`s in views.
        #      View should pass objects to executor.
        # on_commit ensures the executor runs only after the transaction commits.
        # This prevents a race condition when ATOMIC_REQUESTS=True is enabled,
        # where an async worker could try to read the object before it is visible in the DB.
        transaction.on_commit(
            lambda: self.create_executor.execute(
                droplet,
                is_async=self.async_executor,
                backend_region_id=region.backend_id,
                backend_image_id=image.backend_id,
                backend_size_id=size.backend_id,
                ssh_key_uuid=ssh_key.uuid.hex if ssh_key else None,
            )
        )

    @extend_schema(request=None, responses={status.HTTP_202_ACCEPTED: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def start(self, request, uuid=None):
        instance: models.Droplet = self.get_object()
        executors.DropletStartExecutor().execute(instance)
        return response.Response(
            {"status": _("start was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    start_validators = [
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator(models.Droplet.RuntimeStates.OFFLINE),
    ]

    @extend_schema(request=None, responses={status.HTTP_202_ACCEPTED: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def stop(self, request, uuid=None):
        instance: models.Droplet = self.get_object()
        executors.DropletStopExecutor().execute(instance)
        return response.Response(
            {"status": _("stop was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    stop_validators = [
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator(models.Droplet.RuntimeStates.ONLINE),
    ]

    @extend_schema(request=None, responses={status.HTTP_202_ACCEPTED: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def restart(self, request, uuid=None):
        instance: models.Droplet = self.get_object()
        executors.DropletRestartExecutor().execute(instance)
        return response.Response(
            {"status": _("restart was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    restart_validators = [
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator(models.Droplet.RuntimeStates.ONLINE),
    ]

    @extend_schema(
        examples=[
            OpenApiExample(
                request_only=True,
                name="digitalocean-droplet-resize",
                value={
                    "size": "http://example.com/api/digitalocean-sizes/1ee385bc043249498cfeb8c7e3e079f0/"
                },
            )
        ],
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def resize(self, request, uuid=None):
        """
        To resize droplet, submit a POST request to the instance URL, specifying URI of a target size.

        Pass {'disk': true} along with target size in order to perform permanent resizing,
        which allows you to resize your disk space as well as CPU and RAM.
        After increasing the disk size, you will not be able to decrease it.

        Pass {'disk': false} along with target size in order to perform flexible resizing,
        which only upgrades your CPU and RAM. This option is reversible.

        Note that instance must be OFFLINE.
        """
        droplet: models.Droplet = self.get_object()
        serializer = self.get_serializer(droplet, data=request.data)
        serializer.is_valid(raise_exception=True)

        size = serializer.validated_data["size"]
        disk = serializer.validated_data["disk"]

        executors.DropletResizeExecutor.execute(
            droplet,
            disk=disk,
            size=size,
            updated_fields=None,
            is_async=self.async_executor,
        )

        message = _("Droplet {droplet_name} has been scheduled to %s resize.") % (
            disk and _("permanent") or _("flexible")
        )
        event_logger.emit(
            message,
            event_type=EventType.DROPLET_RESIZE_SCHEDULED,
            event_context={"droplet": droplet, "size": size},
            scopes=[droplet, droplet.project, droplet.project.customer],
        )

        droplet.cores = size.cores
        droplet.ram = size.ram

        if disk:
            droplet.disk = size.disk

        droplet.save()

        return response.Response(
            {"detail": _("resizing was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    resize_validators = [core_validators.StateValidator(CoreStates.OK)]
    resize_serializer_class = serializers.DigitalOceanDropletResizeSerializer
