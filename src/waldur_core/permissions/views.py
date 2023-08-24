from rest_framework import viewsets

from . import models, serializers


class RoleViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = ()
    queryset = models.Role.objects.all()
    serializer_class = serializers.RoleSerializer
