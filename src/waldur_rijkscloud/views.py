from __future__ import unicode_literals

from waldur_core.structure import views as structure_views

from . import filters, executors, models, serializers


class ServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.RijkscloudService.objects.all()
    serializer_class = serializers.ServiceSerializer


class ServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.RijkscloudServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer
    filter_class = filters.ServiceProjectLinkFilter


class FlavorViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Flavor.objects.all().order_by('cores', 'ram')
    serializer_class = serializers.FlavorSerializer
    lookup_field = 'uuid'
    filter_class = filters.FlavorFilter


class VolumeViewSet(structure_views.ImportableResourceViewSet):
    queryset = models.Volume.objects.all()
    serializer_class = serializers.VolumeSerializer
    filter_class = filters.VolumeFilter
    create_executor = executors.VolumeCreateExecutor
    pull_executor = executors.VolumePullExecutor
    delete_executor = executors.VolumeDeleteExecutor
    disabled_actions = ['update', 'partial_update']

    importable_resources_backend_method = 'get_volumes_for_import'
    importable_resources_serializer_class = serializers.VolumeImportableSerializer
    import_resource_serializer_class = serializers.VolumeImportSerializer


class InstanceViewSet(structure_views.ImportableResourceViewSet):
    queryset = models.Instance.objects.all()
    serializer_class = serializers.InstanceSerializer
    filter_class = filters.InstanceFilter
    pull_executor = executors.InstancePullExecutor
    create_executor = executors.InstanceCreateExecutor
    delete_executor = executors.InstanceDeleteExecutor
    disabled_actions = ['update', 'partial_update']

    importable_resources_backend_method = 'get_instances_for_import'
    importable_resources_serializer_class = serializers.InstanceImportableSerializer
    import_resource_serializer_class = serializers.InstanceImportSerializer


class NetworkViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Network.objects.all().order_by('settings')
    serializer_class = serializers.NetworkSerializer
    lookup_field = 'uuid'
    filter_class = filters.NetworkFilter


class SubNetViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.SubNet.objects.all().order_by('settings')
    serializer_class = serializers.SubNetSerializer
    lookup_field = 'uuid'
    filter_class = filters.SubNetFilter


class InternalIPViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.InternalIP.objects.all().order_by('settings', 'address')
    serializer_class = serializers.InternalIPSerializer
    lookup_field = 'uuid'
    filter_class = filters.InternalIPFilter


class FloatingIPViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.FloatingIP.objects.all().order_by('settings', 'address')
    serializer_class = serializers.FloatingIPSerializer
    lookup_field = 'uuid'
    filter_class = filters.FloatingIPFilter
