from __future__ import unicode_literals

from django_filters.rest_framework import DjangoFilterBackend
from django.utils.translation import ugettext_lazy as _
from rest_framework import permissions, status, viewsets, exceptions
from rest_framework.decorators import detail_route
from rest_framework.response import Response

from waldur_core.core import permissions as core_permissions, views as core_views
from waldur_core.structure import filters as structure_filters, permissions as structure_permissions

from . import filters, models, serializers, tasks


class InvoiceViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Invoice.objects.order_by('-year', '-month')
    serializer_class = serializers.InvoiceSerializer
    lookup_field = 'uuid'
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.InvoiceFilter

    def _is_invoice_created(invoice):
        if invoice.state != models.Invoice.States.CREATED:
            raise exceptions.ValidationError(_('Notification only for the created invoice can be sent.'))

    @detail_route(methods=['post'])
    def send_notification(self, request, uuid=None):
        invoice = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        link_template = serializer.validated_data.get('link_template')
        tasks.send_invoice_notification.delay(invoice.uuid.hex, link_template)

        return Response({'detail': _('Invoice notification sending has been successfully scheduled.')},
                        status=status.HTTP_200_OK)

    send_notification_serializer_class = serializers.InvoiceNotificationSerializer
    send_notification_permissions = [structure_permissions.is_staff]
    send_notification_validators = [_is_invoice_created]


class PaymentDetailsViewSet(viewsets.ModelViewSet):
    queryset = models.PaymentDetails.objects.order_by('customer')
    serializer_class = serializers.PaymentDetailsSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated, core_permissions.IsAdminOrReadOnly)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.PaymentDetailsFilter
