from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import permissions, viewsets

from waldur_core.structure import filters as structure_filters

from . import filters, models, serializers


class SlurmPackageViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.SlurmPackage.objects.all()
    serializer_class = serializers.SlurmPackageSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend,)
    filter_class = filters.SlurmPackageFilter
