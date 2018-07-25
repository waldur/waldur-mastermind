import logging

from django.conf import settings
from django.views.static import serve
from django_fsm import TransitionNotAllowed
from django.utils.translation import ugettext_lazy as _
from rest_framework import decorators, exceptions, status, response, views

from waldur_core.core import views as core_views
from waldur_core.structure import permissions as structure_permissions

from . import backend, filters, log, models, serializers


logger = logging.getLogger(__name__)


class ExtensionDisabled(exceptions.APIException):
    status_code = status.HTTP_424_FAILED_DEPENDENCY
    default_detail = _('PayPal extension is disabled.')


class CheckExtensionMixin(object):
    """ Raise exception if paypal extension is disabled """

    def initial(self, request, *args, **kwargs):
        if not settings.WALDUR_PAYPAL['ENABLED']:
            raise ExtensionDisabled()
        return super(CheckExtensionMixin, self).initial(request, *args, **kwargs)


class CreateByStaffOrOwnerMixin(object):

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        customer = serializer.validated_data['customer']
        if not structure_permissions._has_owner_access(request.user, customer):
            raise exceptions.PermissionDenied()

        return super(CreateByStaffOrOwnerMixin, self).create(request)


class PaymentView(CheckExtensionMixin, CreateByStaffOrOwnerMixin, core_views.ProtectedViewSet):
    queryset = models.Payment.objects.all()
    serializer_class = serializers.PaymentSerializer
    lookup_field = 'uuid'
    filter_class = filters.PaymentFilter

    def perform_create(self, serializer):
        """
        Create new payment via Paypal gateway
        """

        return_url = serializer.validated_data.pop('return_url')
        cancel_url = serializer.validated_data.pop('cancel_url')

        payment = serializer.save()

        try:
            backend_payment = payment.get_backend().make_payment(
                payment.amount, payment.tax,
                description='Replenish account in Waldur for %s' % payment.customer.name,
                return_url=return_url,
                cancel_url=cancel_url)

            payment.backend_id = backend_payment.payment_id
            payment.approval_url = backend_payment.approval_url
            payment.token = backend_payment.token
            payment.set_created()
            payment.save()

            serializer.instance = payment

            log.event_logger.paypal_payment.info(
                'Created new payment for {customer_name}',
                event_type='payment_creation_succeeded',
                event_context={'payment': payment}
            )

        except backend.PayPalError as e:
            message = 'Unable to create payment because of backend error %s' % e
            logger.warning(message)
            payment.set_erred()
            payment.error_message = message
            payment.save()
            raise exceptions.APIException()

    def get_payment(self, token):
        """
        Find Paypal payment object in the database by token
        and check if current user has access to it.
        :param token: string
        :return: Payment object
        """
        error_message = "Payment with token %s does not exist" % token

        try:
            payment = models.Payment.objects.get(token=token)
        except models.Payment.DoesNotExist:
            raise exceptions.NotFound(error_message)

        if not structure_permissions._has_owner_access(self.request.user, payment.customer):
            raise exceptions.NotFound(error_message)

        return payment

    @decorators.list_route(methods=['POST'])
    def approve(self, request):
        """
        Approve Paypal payment.
        """
        serializer = serializers.PaymentApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        payment_id = serializer.validated_data['payment_id']
        payer_id = serializer.validated_data['payer_id']
        token = serializer.validated_data['token']
        payment = self.get_payment(token)

        try:
            payment.get_backend().approve_payment(payment_id, payer_id)

            payment.set_approved()
            payment.error_message = ''
            payment.save()

            log.event_logger.paypal_payment.info(
                'Payment for {customer_name} has been approved.',
                event_type='payment_approval_succeeded',
                event_context={'payment': payment}
            )
            return response.Response({'detail': 'Payment has been approved.'}, status=status.HTTP_200_OK)

        except backend.PayPalError as e:
            message = 'Unable to approve payment because of backend error %s' % e
            logger.warning(message)
            payment.error_message = message
            payment.save()
            raise exceptions.APIException(message)

        except TransitionNotAllowed:
            message = 'Unable to approve payment because of invalid state.'
            payment.set_erred()
            payment.error_message = message
            payment.save()
            return response.Response({'detail': message}, status=status.HTTP_409_CONFLICT)

    @decorators.list_route(methods=['POST'])
    def cancel(self, request):
        """
        Cancel Paypal payment.
        """
        serializer = serializers.PaymentCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        token = serializer.validated_data['token']
        payment = self.get_payment(token)

        try:
            payment.set_cancelled()
            payment.save()

            log.event_logger.paypal_payment.info(
                'Payment for {customer_name} has been cancelled.',
                event_type='payment_cancel_succeeded',
                event_context={'payment': payment}
            )
            return response.Response({'detail': 'Payment has been cancelled.'}, status=status.HTTP_200_OK)

        except TransitionNotAllowed:
            return response.Response({'detail': 'Unable to cancel payment because of invalid state.'},
                                     status=status.HTTP_409_CONFLICT)


class InvoicesViewSet(CheckExtensionMixin, core_views.ReadOnlyActionsViewSet):
    queryset = models.Invoice.objects.all()
    serializer_class = serializers.InvoiceSerializer
    lookup_field = 'uuid'
    filter_class = filters.InvoiceFilter

    def _serve_pdf(self, request, pdf):
        if not pdf:
            raise exceptions.NotFound("There's no PDF for this invoice.")

        response = serve(request, pdf.name, document_root=settings.MEDIA_ROOT)
        if request.query_params.get('download'):
            filename = pdf.name.split('/')[-1]
            response['Content-Type'] = 'application/pdf'
            response['Content-Disposition'] = 'attachment; filename="{}"'.format(filename)

        return response

    @decorators.detail_route()
    def pdf(self, request, uuid=None):
        return self._serve_pdf(request, self.get_object().pdf)


class InvoiceWebHookViewSet(CheckExtensionMixin, views.APIView):
    authentication_classes = ()
    permission_classes = ()
    serializer_class = serializers.InvoiceUpdateWebHookSerializer
    valid_event_types = [
        'INVOICING.INVOICE.CANCELLED',
        'INVOICING.INVOICE.PAID',
        'INVOICING.INVOICE.REFUNDED',
        'INVOICING.INVOICE.UPDATED',
        # 'INVOICING.INVOICE.CREATED', No need to update created Invoice
    ]

    def post(self, request, *args, **kwargs):
        if request.data.get('event_type') not in self.valid_event_types:
            return response.Response(status=status.HTTP_304_NOT_MODIFIED)

        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return response.Response(status=status.HTTP_200_OK)
