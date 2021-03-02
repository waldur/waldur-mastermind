import datetime
import decimal

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import exceptions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.common.utils import quantize_price

from . import filters, log, models, serializers, tasks, utils


class InvoiceViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Invoice.objects.order_by('-year', '-month')
    serializer_class = serializers.InvoiceSerializer
    lookup_field = 'uuid'
    filter_backends = (
        structure_filters.GenericRoleFilter,
        structure_filters.CustomerAccountingStartDateFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.InvoiceFilter

    def _is_invoice_created(invoice):
        if invoice.state != models.Invoice.States.CREATED:
            raise exceptions.ValidationError(
                _('Notification only for the created invoice can be sent.')
            )

    @action(detail=True, methods=['post'])
    def send_notification(self, request, uuid=None):
        invoice = self.get_object()
        tasks.send_invoice_notification.delay(invoice.uuid.hex)

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

        file = utils.create_invoice_pdf(invoice)
        file_response = HttpResponse(file, content_type='application/pdf')
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

            proof = serializer.validated_data.get('proof')

            if proof:
                payment.proof = proof

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

    @action(detail=True)
    def stats(self, request, uuid=None):
        invoice = self.get_object()
        offerings = {}

        for item in invoice.items.all():
            if not item.resource:
                continue

            resource = item.resource
            offering = resource.offering
            customer = offering.customer
            service_category_title = offering.category.title
            service_provider_name = customer.name
            service_provider_uuid = customer.serviceprovider.uuid.hex

            if offering.uuid.hex not in offerings.keys():
                offerings[offering.uuid.hex] = {
                    'offering_name': offering.name,
                    'aggregated_cost': item.total,
                    'service_category_title': service_category_title,
                    'service_provider_name': service_provider_name,
                    'service_provider_uuid': service_provider_uuid,
                }
            else:
                offerings[offering.uuid.hex]['aggregated_cost'] += item.total

        queryset = [dict(uuid=key, **details) for (key, details) in offerings.items()]

        for item in queryset:
            item['aggregated_cost'] = quantize_price(
                decimal.Decimal(item['aggregated_cost'])
            )

        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(page)

    @action(detail=False)
    def growth(self, request):
        if not self.request.user.is_staff and not request.user.is_support:
            raise exceptions.PermissionDenied()

        customers = structure_models.Customer.objects.all()
        customers = structure_filters.AccountingStartDateFilter().filter_queryset(
            request, customers, self
        )

        customers_count = 4
        if 'customers_count' in request.query_params:
            try:
                customers_count = int(request.query_params['customers_count'])
            except ValueError:
                raise exceptions.ValidationError('customers_count is not a number')

        if customers_count > 20:
            raise exceptions.ValidationError(
                'customers_count should not be greater than 20'
            )

        is_accounting_mode = request.query_params.get('accounting_mode') == 'accounting'

        today = datetime.date.today()
        current_month = today - relativedelta(months=12)

        majors = list(
            models.Invoice.objects.filter(
                customer__in=customers, created__gte=current_month
            )
            .values('customer_id')
            .annotate(total=Sum('current_cost'))
            .order_by('-total')
            .values_list('customer_id', flat=True)[:customers_count]
        )

        minors = customers.exclude(id__in=majors)

        customer_periods = {}
        total_periods = {}
        other_periods = {}

        for i in range(13):
            invoices = models.Invoice.objects.filter(
                year=current_month.year, month=current_month.month,
            )
            key = f'{current_month.year}-{current_month.month}'
            row = customer_periods[key] = {}
            subtotal = 0
            for invoice in invoices.filter(customer_id__in=majors):
                value = is_accounting_mode and invoice.price or invoice.total
                subtotal += value
                row[invoice.customer.uuid.hex] = value
            other_periods[key] = sum(
                is_accounting_mode and invoice.price or invoice.total
                for invoice in invoices.filter(customer_id__in=minors)
            )
            total_periods[key] = subtotal + other_periods[key]
            current_month += relativedelta(months=1)

        result = {
            'periods': total_periods.keys(),
            'total_periods': total_periods.values(),
            'other_periods': other_periods.values(),
            'customer_periods': [
                {
                    'name': customer.name,
                    'periods': [
                        customer_periods[period].get(customer.uuid.hex, 0)
                        for period in total_periods.keys()
                    ],
                }
                for customer in structure_models.Customer.objects.filter(id__in=majors)
            ],
        }

        return Response(result, status=status.HTTP_200_OK)


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
