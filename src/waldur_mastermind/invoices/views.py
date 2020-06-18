from celery import chain
from django.http import Http404, HttpResponse
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import exceptions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions

from . import filters, models, serializers, tasks


class InvoiceViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Invoice.objects.order_by('-year', '-month')
    serializer_class = serializers.InvoiceSerializer
    lookup_field = 'uuid'
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.InvoiceFilter

    def _is_invoice_created(invoice):
        if invoice.state != models.Invoice.States.CREATED:
            raise exceptions.ValidationError(
                _('Notification only for the created invoice can be sent.')
            )

    @action(detail=True, methods=['post'])
    def send_notification(self, request, uuid=None):
        invoice = self.get_object()
        serialized_invoice = core_utils.serialize_instance(invoice)
        chain(
            tasks.create_invoice_pdf.si(serialized_invoice),
            tasks.send_invoice_notification.si(invoice.uuid.hex),
        )()

        return Response(
            {
                'detail': _(
                    'Invoice notification sending has been successfully scheduled.'
                )
            },
            status=status.HTTP_200_OK,
        )

    send_notification_permissions = [structure_permissions.is_staff]
    send_notification_validators = [_is_invoice_created]

    @action(detail=True)
    def pdf(self, request, uuid=None):
        invoice = self.get_object()
        if not invoice.has_file():
            tasks.create_invoice_pdf.delay(core_utils.serialize_instance(invoice))
            raise Http404()

        file_response = HttpResponse(invoice.file, content_type='application/pdf')
        filename = invoice.get_filename()
        file_response[
            'Content-Disposition'
        ] = 'attachment; filename="{filename}"'.format(filename=filename)
        return file_response

    @action(detail=True, methods=['post'])
    def paid(self, request, uuid=None):
        invoice = self.get_object()
        invoice.state = models.Invoice.States.PAID
        invoice.save(update_fields=['state'])
        return Response(status=status.HTTP_200_OK)

    paid_permissions = [structure_permissions.is_staff]
    paid_validators = [core_validators.StateValidator(models.Invoice.States.CREATED)]


class PaymentProfileViewSet(core_views.ActionsViewSet):
    lookup_field = 'uuid'
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
        filters.PaymentProfileFilterBackend,
    )
    filterset_class = filters.PaymentProfileFilter
    create_permissions = (
        update_permissions
    ) = partial_update_permissions = destroy_permissions = enable_permissions = [
        structure_permissions.is_staff
    ]
    queryset = models.PaymentProfile.objects.all()
    serializer_class = serializers.PaymentProfileSerializer

    @action(detail=True, methods=['post'])
    def enable(self, request, uuid=None):
        profile = self.get_object()
        profile.is_active = True
        profile.save(update_fields=['is_active'])

        return Response(
            {'detail': _('Payment profile has been enabled.')},
            status=status.HTTP_200_OK,
        )
