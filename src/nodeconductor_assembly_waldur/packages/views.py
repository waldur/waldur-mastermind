from rest_framework import viewsets, mixins, permissions, response, status, filters as rf_filters
from rest_framework.decorators import list_route

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

    def get_serializer_class(self):
        if self.action == 'extend':
            return serializers.OpenStackPackageExtendSerializer
        return super(OpenStackPackageViewSet, self).get_serializer_class()

    @list_route(methods=['post'])
    def extend(self, request, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_package = serializer.save()
        executors.OpenStackPackageExtendExecutor.execute(new_package.tenant)

        return response.Response({'detail': 'OpenStack package extend has been scheduled'},
                                 status=status.HTTP_202_ACCEPTED)
