from decimal import Decimal
import logging

from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import serializers as core_serializers
from waldur_core.structure.models import VATException

from . import models


logger = logging.getLogger(__name__)


class PaymentSerializer(core_serializers.AugmentedSerializerMixin,
                        serializers.HyperlinkedModelSerializer):

    amount = serializers.DecimalField(max_digits=9, decimal_places=2)
    state = serializers.ReadOnlyField(source='get_state_display')
    return_url = serializers.CharField(write_only=True)
    cancel_url = serializers.CharField(write_only=True)

    class Meta(object):
        model = models.Payment

        fields = (
            'url', 'uuid', 'created', 'modified', 'state',
            'amount', 'customer', 'return_url', 'cancel_url', 'approval_url', 'error_message', 'tax'
        )

        read_only_fields = ('approval_url', 'error_message', 'tax')
        protected_fields = ('customer', 'amount', 'return_url', 'cancel_url')

        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'paypal-payment-detail'},
            'customer': {'lookup_field': 'uuid', 'view_name': 'customer-detail'},
        }

    def create(self, validated_data):
        customer = validated_data['customer']
        amount = validated_data['amount']

        try:
            rate = customer.get_vat_rate() or 0
        except (NotImplemented, VATException) as e:
            rate = 0
            logger.warning('Unable to compute VAT rate for customer with UUID %s, error is %s',
                           customer.uuid, e)
        validated_data['tax'] = Decimal(rate) / Decimal(100) * amount

        return super(PaymentSerializer, self).create(validated_data)


class PaymentApproveSerializer(serializers.Serializer):
    payment_id = serializers.CharField()
    payer_id = serializers.CharField()
    token = serializers.CharField()


class PaymentCancelSerializer(serializers.Serializer):
    token = serializers.CharField()


class InvoiceItemSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.InvoiceItem
        fields = ('price', 'tax', 'unit_price', 'quantity', 'unit_of_measure', 'name', 'start', 'end')


class InvoiceSerializer(core_serializers.AugmentedSerializerMixin,
                        serializers.HyperlinkedModelSerializer):

    pdf = serializers.SerializerMethodField()
    items = InvoiceItemSerializer(many=True, read_only=True)
    payment_url = serializers.SerializerMethodField()
    issuer_details = serializers.JSONField()
    customer_details = serializers.JSONField(source='payment_details')

    class Meta(object):
        model = models.Invoice
        fields = (
            'url', 'uuid', 'total', 'price', 'tax', 'pdf', 'backend_id', 'issuer_details',
            'invoice_date', 'end_date', 'state', 'items', 'payment_url', 'customer_details',
            'customer', 'customer_uuid', 'customer_name', 'year', 'month', 'number',
        )
        related_paths = ('customer',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'paypal-invoice-detail'},
            'customer': {'lookup_field': 'uuid'}
        }

    def get_payment_url(self, invoice):
        backend = invoice.get_backend()
        return backend.get_payment_view_url(invoice.backend_id) if invoice.backend_id else None

    def get_pdf(self, invoice):
        """
        Format URL to PDF view if file is specified
        """
        if invoice.pdf:
            return reverse('paypal-invoice-pdf',
                           kwargs={'uuid': invoice.uuid},
                           request=self.context['request'])


class InvoiceUpdateWebHookSerializer(serializers.Serializer):

    @transaction.atomic()
    def save(self, **kwargs):
        backend_id = self.initial_data['resource']['id']
        status = self.initial_data['resource']['status']

        try:
            invoice = models.Invoice.objects.get(backend_id=backend_id)
        except models.Invoice.DoesNotExist:
            raise serializers.ValidationError({'backend_id': _('Invoice with id "%s" cannot be found.') % backend_id})

        invoice.state = status
        invoice.save(update_fields=['state'])
        return invoice
