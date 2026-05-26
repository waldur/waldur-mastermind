from django.db import transaction
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from rest_framework import decorators, response, status, viewsets

from waldur_core.core import exceptions as core_exceptions
from waldur_core.core.serializers import StatusSerializer
from waldur_core.core import validators as core_validators
from waldur_core.core.enums import CoreStates
from waldur_core.structure import views as structure_views

from . import executors, filters, models, serializers


class RegionViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Region.objects.all()
    serializer_class = serializers.AwsRegionSerializer
    filterset_class = filters.RegionFilter
    lookup_field = "uuid"


class ImageViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Image.objects.all()
    serializer_class = serializers.AwsImageSerializer
    filterset_class = filters.ImageFilter
    lookup_field = "uuid"


class SizeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.Size.objects.all()
    serializer_class = serializers.AwsSizeSerializer
    filterset_class = filters.SizeFilter
    lookup_field = "uuid"


class InstanceViewSet(structure_views.ResourceViewSet):
    queryset = models.Instance.objects.all().order_by("name")
    filterset_class = filters.InstanceFilter
    serializer_class = serializers.AwsInstanceSerializer
    create_executor = executors.InstanceCreateExecutor

    delete_executor = executors.InstanceDeleteExecutor
    destroy_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED)
    ]

    def perform_create(self, serializer):
        instance: models.Instance = serializer.save()
        volume = instance.volume_set.first()

        # on_commit ensures the executor runs only after the transaction commits.
        # This prevents a race condition when ATOMIC_REQUESTS=True is enabled,
        # where an async worker could try to read the object before it is visible in the DB.
        transaction.on_commit(
            lambda: self.create_executor.execute(
                instance,
                image=serializer.validated_data.get("image"),
                size=serializer.validated_data.get("size"),
                ssh_key=serializer.validated_data.get("ssh_public_key"),
                volume=volume,
            )
        )

    @extend_schema(request=None, responses={status.HTTP_202_ACCEPTED: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def start(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        executors.InstanceStartExecutor().execute(instance)
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
        instance: models.Instance = self.get_object()
        executors.InstanceStopExecutor().execute(instance)
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
        instance: models.Instance = self.get_object()
        executors.InstanceRestartExecutor().execute(instance)
        return response.Response(
            {"status": _("restart was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    restart_validators = [
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator("running"),
    ]

    @extend_schema(responses={status.HTTP_202_ACCEPTED: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def resize(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        new_size = serializer.validated_data.get("size")
        executors.InstanceResizeExecutor().execute(instance, size=new_size)
        return response.Response(
            {"status": _("resize was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    resize_validators = [core_validators.StateValidator(CoreStates.OK)]
    resize_serializer_class = serializers.AwsInstanceResizeSerializer


class VolumeViewSet(structure_views.ResourceViewSet):
    queryset = models.Volume.objects.all().order_by("name")
    serializer_class = serializers.AwsVolumeSerializer
    create_executor = executors.VolumeCreateExecutor
    delete_executor = executors.VolumeDeleteExecutor

    def _has_instance(volume):
        if not volume.instance:
            raise core_exceptions.IncorrectStateException(
                _("Volume is already detached.")
            )

    @extend_schema(request=None, responses={status.HTTP_202_ACCEPTED: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def detach(self, request, uuid=None):
        executors.VolumeDetachExecutor.execute(self.get_object())

    detach_validators = [
        core_validators.StateValidator(CoreStates.OK),
        _has_instance,
    ]

    @extend_schema(responses={status.HTTP_202_ACCEPTED: None})
    @decorators.action(detail=True, methods=["post"])
    def attach(self, request, volume, uuid=None):
        serializer = self.get_serializer(volume, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.VolumeAttachExecutor.execute(volume)

    attach_validators = [core_validators.StateValidator(CoreStates.OK)]
    attach_serializer_class = serializers.AwsVolumeAttachSerializer
