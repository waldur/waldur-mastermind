from __future__ import unicode_literals

from rest_framework import viewsets, permissions
from django_filters.rest_framework import DjangoFilterBackend

from nodeconductor.structure import filters as structure_filters

from . import serializers, models, filters


class ExpertProviderViewSet(viewsets.ModelViewSet):
    queryset = models.ExpertProvider.objects.all()
    serializer_class = serializers.ExpertOrganizationSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.ExportProviderFilter
