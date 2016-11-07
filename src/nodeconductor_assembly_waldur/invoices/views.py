from rest_framework import viewsets, permissions, filters as rf_filters

from nodeconductor.structure import filters as structure_filters

from . import filters, models, serializers


class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.Invoice.objects.order_by('-year', '-month')
    serializer_class = serializers.InvoiceSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated, permissions.DjangoObjectPermissions)
    filter_backends = (structure_filters.GenericRoleFilter, rf_filters.DjangoFilterBackend,)
    filter_class = filters.InvoiceFilter
