from rest_framework import viewsets, mixins, permissions, filters as rf_filters

from nodeconductor.core import mixins as core_mixins
from nodeconductor.structure import filters as structure_filters

from . import filters, models, serializers, executors


class PackageTemplateViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.PackageTemplate.objects.all()
    serializer_class = serializers.PackageTemplateSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (rf_filters.DjangoFilterBackend,)
    filter_class = filters.PackageTemplateFilter


class OpenStackPackageViewSet(core_mixins.CreateExecutorMixin, mixins.CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    queryset = models.OpenStackPackage.objects.all()
    serializer_class = serializers.OpenStackPackageSerializer
    lookup_field = 'uuid'
    filter_backends = (structure_filters.GenericRoleFilter, rf_filters.DjangoFilterBackend)
    filter_class = filters.OpenStackPackageFilter
    permission_classes = (permissions.IsAuthenticated, permissions.DjangoObjectPermissions)
    create_executor = executors.OpenStackPackageCreateExecutor
