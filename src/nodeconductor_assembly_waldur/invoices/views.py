from rest_framework import filters as rf_filters, permissions, status, viewsets, exceptions
from rest_framework.decorators import detail_route
from rest_framework.response import Response

from nodeconductor.core import permissions as core_permissions
from nodeconductor.structure import filters as structure_filters

from . import filters, models, serializers, tasks


class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.Invoice.objects.order_by('-year', '-month')
    serializer_class = serializers.InvoiceSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated, permissions.DjangoObjectPermissions)
    filter_backends = (structure_filters.GenericRoleFilter, rf_filters.DjangoFilterBackend,)
    filter_class = filters.InvoiceFilter

    def get_serializer_class(self):
        if self.action == 'send_notification':
            return serializers.InvoiceNotificationSerializer
        return super(InvoiceViewSet, self).get_serializer_class()

    @detail_route(methods=['post'], permission_classes=[permissions.IsAdminUser])
    def send_notification(self, request, uuid=None):
        invoice = self.get_object()
        if invoice.state != models.Invoice.States.CREATED:
            raise exceptions.ValidationError('Notification only for the created invoice can be sent.')
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        link_template = serializer.validated_data.get('link_template')
        tasks.send_invoice_notification.delay(invoice.uuid.hex, link_template)

        return Response({'detail': "Invoice notification sending has been successfully scheduled."},
                        status=status.HTTP_200_OK)


class PaymentDetailsViewSet(viewsets.ModelViewSet):
    queryset = models.PaymentDetails.objects.order_by('customer')
    serializer_class = serializers.PaymentDetailsSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated, core_permissions.IsAdminOrReadOnly,
                          permissions.DjangoObjectPermissions)
    filter_backends = (structure_filters.GenericRoleFilter, rf_filters.DjangoFilterBackend)
    filter_class = filters.PaymentDetailsFilter
