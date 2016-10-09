from rest_framework import permissions as rf_permissions
from rest_framework import viewsets

from nodeconductor_assembly_waldur.packages import models
from nodeconductor_assembly_waldur.packages import serializers


class PackageTemplateViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.PackageTemplate.objects.all()
    serializer_class = serializers.PackageTemplateSerializer
    lookup_field = 'uuid'
    permission_classes = (rf_permissions.IsAuthenticated,)
