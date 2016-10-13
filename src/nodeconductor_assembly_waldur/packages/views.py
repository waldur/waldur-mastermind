from rest_framework import viewsets, mixins, permissions, filters as rf_filters

from nodeconductor.structure import filters as structure_filters

from . import models, serializers


class PackageTemplateViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.PackageTemplate.objects.all()
    serializer_class = serializers.PackageTemplateSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated,)


class OpenStackPackageViewSet(mixins.CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    queryset = models.OpenStackPackage.objects.all()
    serializer_class = serializers.OpenStackPackageSerializer
    lookup_field = 'uuid'
    filter_backends = (structure_filters.GenericRoleFilter, rf_filters.DjangoFilterBackend)
    permission_classes = (permissions.IsAuthenticated, permissions.DjangoObjectPermissions)
