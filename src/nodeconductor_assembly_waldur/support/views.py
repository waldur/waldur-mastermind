from rest_framework import viewsets, filters as rf_filters, permissions

from nodeconductor.structure import filters as structure_filters

from . import filters, models, serializers


class IssueViewSet(viewsets.ModelViewSet):
    queryset = models.Issue.objects.all()
    lookup_field = 'uuid'
    serializer_class = serializers.IssueSerializer
    permission_classes = (
        permissions.IsAuthenticated,
        permissions.DjangoObjectPermissions,
    )
    filter_backends = (
        structure_filters.GenericRoleFilter,
        rf_filters.DjangoFilterBackend,
        filters.IssueResourceFilterBackend,
    )
    filter_class = filters.IssueFilter
