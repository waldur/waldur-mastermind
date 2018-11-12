from django.utils.translation import gettext_lazy as _
from rest_framework import decorators, viewsets, serializers as rf_serializers, response, status

from waldur_core.core import exceptions as core_exceptions, validators as core_validators
from waldur_core.structure import views as structure_views

from . import filters, models, serializers, executors


class AmazonServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.AWSService.objects.all()
    serializer_class = serializers.ServiceSerializer
    import_serializer_class = serializers.InstanceImportSerializer

    def get_import_context(self):
        return {'resource_type': self.request.query_params.get('resource_type')}

    def get_serializer_class(self):
        from waldur_core.structure import SupportedServices

        if self.request.method == 'POST':
            resource_type = self.request.data.get('type')
            if resource_type == SupportedServices.get_name_for_model(models.Instance):
                return serializers.InstanceImportSerializer
            elif resource_type == SupportedServices.get_name_for_model(models.Volume):
                return serializers.VolumeImportSerializer
        return super(AmazonServiceViewSet, self).get_serializer_class()


class AmazonServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.AWSServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer


class RegionViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Region.objects.all()
    serializer_class = serializers.RegionSerializer
    filter_class = filters.RegionFilter
    lookup_field = 'uuid'


class ImageViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Image.objects.all()
    serializer_class = serializers.ImageSerializer
    filter_class = filters.ImageFilter
    lookup_field = 'uuid'


class SizeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.Size.objects.all()
    serializer_class = serializers.SizeSerializer
    filter_class = filters.SizeFilter
    lookup_field = 'uuid'


class InstanceViewSet(structure_views.ResourceViewSet):
    queryset = models.Instance.objects.all()
    filter_class = filters.InstanceFilter
    serializer_class = serializers.InstanceSerializer
    create_executor = executors.InstanceCreateExecutor

    delete_executor = executors.InstanceDeleteExecutor
    destroy_validators = [core_validators.StateValidator(models.Instance.States.OK, models.Instance.States.ERRED)]

    def perform_create(self, serializer):
        instance = serializer.save()
        volume = instance.volume_set.first()

        self.create_executor.execute(
            instance,
            image=serializer.validated_data.get('image'),
            size=serializer.validated_data.get('size'),
            ssh_key=serializer.validated_data.get('ssh_public_key'),
            volume=volume
        )

    @decorators.detail_route(methods=['post'])
    def start(self, request, uuid=None):
        instance = self.get_object()
        executors.InstanceStartExecutor().execute(instance)
        return response.Response({'status': _('start was scheduled')}, status=status.HTTP_202_ACCEPTED)

    start_validators = [core_validators.StateValidator(models.Instance.States.OK),
                        core_validators.RuntimeStateValidator('stopped')]
    start_serializer_class = rf_serializers.Serializer

    @decorators.detail_route(methods=['post'])
    def stop(self, request, uuid=None):
        instance = self.get_object()
        executors.InstanceStopExecutor().execute(instance)
        return response.Response({'status': _('stop was scheduled')}, status=status.HTTP_202_ACCEPTED)

    stop_validators = [core_validators.StateValidator(models.Instance.States.OK),
                       core_validators.RuntimeStateValidator('running')]
    stop_serializer_class = rf_serializers.Serializer

    @decorators.detail_route(methods=['post'])
    def restart(self, request, uuid=None):
        instance = self.get_object()
        executors.InstanceRestartExecutor().execute(instance)
        return response.Response({'status': _('restart was scheduled')}, status=status.HTTP_202_ACCEPTED)

    restart_validators = [core_validators.StateValidator(models.Instance.States.OK),
                          core_validators.RuntimeStateValidator('running')]
    restart_serializer_class = rf_serializers.Serializer

    @decorators.detail_route(methods=['post'])
    def resize(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        new_size = serializer.validated_data.get('size')
        executors.InstanceResizeExecutor().execute(instance, size=new_size)
        return response.Response({'status': _('resize was scheduled')}, status=status.HTTP_202_ACCEPTED)

    resize_validators = [core_validators.StateValidator(models.Instance.States.OK)]
    resize_serializer_class = serializers.InstanceResizeSerializer


class VolumeViewSet(structure_views.ResourceViewSet):
    queryset = models.Volume.objects.all()
    serializer_class = serializers.VolumeSerializer
    create_executor = executors.VolumeCreateExecutor
    delete_executor = executors.VolumeDeleteExecutor

    def _has_instance(volume):
        if not volume.instance:
            raise core_exceptions.IncorrectStateException(_('Volume is already detached.'))

    @decorators.detail_route(methods=['post'])
    def detach(self, request, uuid=None):
        executors.VolumeDetachExecutor.execute(self.get_object())

    detach_validators = [core_validators.StateValidator(models.Volume.States.OK), _has_instance]
    detach_serializer_class = rf_serializers.Serializer

    @decorators.detail_route(methods=['post'])
    def attach(self, request, volume, uuid=None):
        serializer = self.get_serializer(volume, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.VolumeAttachExecutor.execute(volume)

    attach_validators = [core_validators.StateValidator(models.Volume.States.OK)]
    attach_serializer_class = serializers.VolumeAttachSerializer
