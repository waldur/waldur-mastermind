from django.conf import settings
from rest_framework import serializers
from nodeconductor.core import serializers as core_serializers

from . import models


class OpenStackItemSerializer(serializers.HyperlinkedModelSerializer):
    tax = serializers.DecimalField(max_digits=15, decimal_places=7)
    total = serializers.DecimalField(max_digits=15, decimal_places=7)

    class Meta(object):
        model = models.OpenStackItem
        fields = ('package', 'name', 'price', 'tax', 'total', 'daily_price', 'start', 'end', 'usage_days')
        extra_kwargs = {
            'package': {'lookup_field': 'uuid', 'view_name': 'openstack-package-detail'},
        }


class InvoiceSerializer(serializers.HyperlinkedModelSerializer):
    price = serializers.DecimalField(max_digits=15, decimal_places=7)
    tax = serializers.DecimalField(max_digits=15, decimal_places=7)
    total = serializers.DecimalField(max_digits=15, decimal_places=7)
    openstack_items = OpenStackItemSerializer(many=True)
    issuer_details = serializers.SerializerMethodField()
    customer_details = serializers.SerializerMethodField()
    due_date = serializers.DateField()

    class Meta(object):
        model = models.Invoice
        fields = (
            'url', 'uuid', 'number', 'customer', 'price', 'tax', 'total', 'openstack_items', 'state', 'year', 'month',
            'issuer_details', 'customer_details', 'invoice_date', 'due_date',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
        }

    def get_issuer_details(self, invoice):
        return settings.INVOICES['ISSUER_DETAILS']

    def get_customer_details(self, invoice):
        try:
            payment_details = models.PaymentDetails.objects.get(customer=invoice.customer)
        except models.PaymentDetails.DoesNotExist:
            return
        return {
            'company': payment_details.company,
            'address': payment_details.address,
            'country': payment_details.country,
            'email': payment_details.email,
            'postal': payment_details.postal,
            'phone': payment_details.phone,
            'bank': payment_details.bank,
        }


class InvoiceNotificationSerializer(serializers.Serializer):
    link_template = serializers.URLField(help_text='The template must include {uuid} parameter '
                                                   'e.g. http://example.com/invoice/{uuid}')

    def validate_link_template(self, link_template):
        if '{uuid}' not in link_template:
            raise serializers.ValidationError("Link template must include '{uuid}' parameter.")

        return link_template


class PaymentDetailsSerializer(core_serializers.AugmentedSerializerMixin,
                               serializers.HyperlinkedModelSerializer):

    type = serializers.ChoiceField(choices=[(t, t) for t in settings.INVOICES['COMPANY_TYPES']])

    class Meta(object):
        model = models.PaymentDetails
        fields = (
            'url', 'uuid', 'customer', 'company', 'type', 'address',
            'country', 'email', 'postal', 'phone', 'bank', 'account',
            'default_tax_percent',
        )
        protected_fields = ('customer',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'payment-details-detail'},
            'customer': {'lookup_field': 'uuid'},
        }
