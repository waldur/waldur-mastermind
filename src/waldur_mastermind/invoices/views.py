from celery import chain
from django.db import transaction
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

from . import filters, log, models, serializers, tasks


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

    @transaction.atomic
    @action(detail=True, methods=['post'])
    def paid(self, request, uuid=None):
        invoice = self.get_object()

        if request.data:
            serializer = serializers.PaidSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            try:
                profile = models.PaymentProfile.objects.get(
                    is_active=True, organization=invoice.customer
                )
            except models.PaymentProfile.DoesNotExist:
                raise exceptions.ValidationError(
                    _('The active profile for this customer does not exist.')
                )

            payment = models.Payment.objects.create(
                date_of_payment=serializer.validated_data['date'],
                sum=invoice.total_current,
                profile=profile,
                invoice=invoice,
            )

            payment.proof = serializer.validated_data['proof']
            payment.save()

            log.event_logger.invoice.info(
                'Payment for invoice ({month}/{year}) has been added."',
                event_type='payment_created',
                event_context={
                    'month': invoice.month,
                    'year': invoice.year,
                    'customer': invoice.customer,
                },
            )

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


class PaymentViewSet(core_views.ActionsViewSet):
    lookup_field = 'uuid'
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.PaymentFilter
    create_permissions = (
        update_permissions
    ) = (
        partial_update_permissions
    ) = (
        destroy_permissions
    ) = link_to_invoice_permissions = unlink_from_invoice_permissions = [
        structure_permissions.is_staff
    ]
    queryset = models.Payment.objects.all()
    serializer_class = serializers.PaymentSerializer

    @action(detail=True, methods=['post'])
    def link_to_invoice(self, request, uuid=None):
        payment = self.get_object()
        serializer = self.get_serializer_class()(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = serializer.validated_data['invoice']

        if invoice.customer != payment.profile.organization:
            raise exceptions.ValidationError(
                _('The passed invoice does not belong to the selected customer.')
            )

        payment.invoice = invoice
        payment.save(update_fields=['invoice'])

        log.event_logger.invoice.info(
            'Payment for invoice ({month}/{year}) has been added.',
            event_type='payment_created',
            event_context={
                'month': invoice.month,
                'year': invoice.year,
                'customer': invoice.customer,
            },
        )

        return Response(
            {'detail': _('An invoice has been linked to payment.')},
            status=status.HTTP_200_OK,
        )

    def _link_to_invoice_exists(payment):
        if payment.invoice:
            raise exceptions.ValidationError(_('Link to an invoice exists.'))

    link_to_invoice_validators = [_link_to_invoice_exists]
    link_to_invoice_serializer_class = serializers.LinkToInvoiceSerializer

    def _link_to_invoice_does_not_exist(payment):
        if not payment.invoice:
            raise exceptions.ValidationError(_('Link to an invoice does not exist.'))

    @action(detail=True, methods=['post'])
    def unlink_from_invoice(self, request, uuid=None):
        payment = self.get_object()
        invoice = payment.invoice
        payment.invoice = None
        payment.save(update_fields=['invoice'])

        log.event_logger.invoice.info(
            'Payment for invoice ({month}/{year}) has been removed.',
            event_type='payment_removed',
            event_context={
                'month': invoice.month,
                'year': invoice.year,
                'customer': invoice.customer,
            },
        )

        return Response(
            {'detail': _('An invoice has been unlinked from payment.')},
            status=status.HTTP_200_OK,
        )

    unlink_from_invoice_validators = [_link_to_invoice_does_not_exist]

    def perform_create(self, serializer):
        super(PaymentViewSet, self).perform_create(serializer)
        payment = serializer.instance
        log.event_logger.payment.info(
            'Payment for {customer_name} in the amount of {amount} has been added.',
            event_type='payment_added',
            event_context={
                'amount': payment.sum,
                'customer': payment.profile.organization,
            },
        )

    def perform_destroy(self, instance):
        customer = instance.profile.organization
        amount = instance.sum
        super(PaymentViewSet, self).perform_destroy(instance)

        log.event_logger.payment.info(
            'Payment for {customer_name} in the amount of {amount} has been removed.',
            event_type='payment_removed',
            event_context={'amount': amount, 'customer': customer,},
        )
