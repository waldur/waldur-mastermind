from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from nodeconductor.core import serializers as core_serializers

from . import models


class InvoiceItemSerializer(serializers.HyperlinkedModelSerializer):
    tax = serializers.DecimalField(max_digits=15, decimal_places=7)
    total = serializers.DecimalField(max_digits=15, decimal_places=7)

    class Meta(object):
        model = models.InvoiceItem
        fields = ('name', 'price', 'tax', 'total', 'unit_price', 'unit',
                  'start', 'end', 'usage_days', 'product_code',
                  'article_code', 'project_name', 'project_uuid',)


class OpenStackItemSerializer(InvoiceItemSerializer):
    tenant_name = serializers.ReadOnlyField(source='get_tenant_name')
    tenant_uuid = serializers.ReadOnlyField(source='get_tenant_uuid')
    template_name = serializers.ReadOnlyField(source='get_template_name')
    template_uuid = serializers.ReadOnlyField(source='get_template_uuid')
    template_category = serializers.ReadOnlyField(source='get_template_category')

    class Meta(InvoiceItemSerializer.Meta):
        model = models.OpenStackItem
        fields = InvoiceItemSerializer.Meta.fields + ('package', 'tenant_name', 'tenant_uuid',
                                                      'template_name', 'template_uuid', 'template_category')
        extra_kwargs = {
            'package': {'lookup_field': 'uuid', 'view_name': 'openstack-package-detail'},
        }


class OfferingItemSerializer(InvoiceItemSerializer):
    offering_type = serializers.ReadOnlyField(source='get_offering_type')

    class Meta(InvoiceItemSerializer.Meta):
        model = models.OfferingItem
        fields = InvoiceItemSerializer.Meta.fields + ('offering', 'offering_type')
        extra_kwargs = {
            'offering': {'lookup_field': 'uuid', 'view_name': 'support-offering-detail'},
        }


class GenericItemSerializer(InvoiceItemSerializer):
    class Meta(InvoiceItemSerializer.Meta):
        model = models.GenericInvoiceItem


class InvoiceSerializer(core_serializers.RestrictedSerializerMixin,
                        serializers.HyperlinkedModelSerializer):
    price = serializers.DecimalField(max_digits=15, decimal_places=7)
    tax = serializers.DecimalField(max_digits=15, decimal_places=7)
    total = serializers.DecimalField(max_digits=15, decimal_places=7)
    openstack_items = OpenStackItemSerializer(many=True)
    offering_items = OfferingItemSerializer(many=True)
    generic_items = GenericItemSerializer(many=True)
    issuer_details = serializers.SerializerMethodField()
    customer_details = serializers.SerializerMethodField()
    due_date = serializers.DateField()

    class Meta(object):
        model = models.Invoice
        fields = (
            'url', 'uuid', 'number', 'customer', 'price', 'tax', 'total',
            'state', 'year', 'month', 'issuer_details', 'customer_details', 'invoice_date', 'due_date',
            'openstack_items', 'offering_items', 'generic_items',
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
    link_template = serializers.URLField(help_text=_('The template must include {uuid} parameter '
                                                     'e.g. http://example.com/invoice/{uuid}'))

    def validate_link_template(self, link_template):
        if '{uuid}' not in link_template:
            raise serializers.ValidationError(_("Link template must include '{uuid}' parameter."))

        return link_template


class PaymentDetailsSerializer(core_serializers.AugmentedSerializerMixin,
                               serializers.HyperlinkedModelSerializer):

    type = serializers.ChoiceField(choices=[(t, t) for t in settings.INVOICES['COMPANY_TYPES']],
                                   allow_blank=True,
                                   required=False)

    class Meta(object):
        model = models.PaymentDetails
        fields = (
            'url', 'uuid', 'customer', 'company', 'type', 'address',
            'country', 'email', 'postal', 'phone', 'bank', 'account',
            'default_tax_percent', 'accounting_start_date', 'is_billable',
        )
        protected_fields = ('customer',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
        }

    def get_fields(self):
        fields = super(PaymentDetailsSerializer, self).get_fields()
        if isinstance(self.instance, models.PaymentDetails):
            fields['accounting_start_date'].read_only = self.instance.is_billable()
        return fields


class InvoiceItemReportSerializer(serializers.ModelSerializer):
    invoice_number = serializers.ReadOnlyField(source='invoice.number')
    invoice_uuid = serializers.ReadOnlyField(source='invoice.uuid')
    invoice_year = serializers.ReadOnlyField(source='invoice.year')
    invoice_month = serializers.ReadOnlyField(source='invoice.month')
    invoice_date = serializers.ReadOnlyField(source='invoice.invoice_date')
    due_date = serializers.ReadOnlyField(source='invoice.due_date')
    customer_uuid = serializers.ReadOnlyField(source='invoice.customer.uuid')
    customer_name = serializers.ReadOnlyField(source='invoice.customer.name')

    class Meta(object):
        model = models.OpenStackItem
        fields = (
            'customer_uuid', 'customer_name',
            'project_uuid', 'project_name',
            'invoice_uuid', 'invoice_number',
            'invoice_year', 'invoice_month',
            'invoice_date', 'due_date',
            'invoice_price', 'invoice_tax', 'invoice_total',
            'name', 'article_code', 'product_code',
            'price', 'tax', 'total', 'unit_price', 'unit',
            'start', 'end', 'usage_days',
        )
        decimal_fields = (
            'price', 'tax', 'total', 'unit_price',
            'invoice_price', 'invoice_tax', 'invoice_total'
        )
        decimal_fields_extra_kwargs = {
            'invoice_price': {
                'source': 'invoice.price',
            },
            'invoice_tax': {
                'source': 'invoice.tax',
            },
            'invoice_total': {
                'source': 'invoice.total',
            },
        }

    def build_field(self, field_name, info, model_class, nested_depth):
        if field_name in self.Meta.decimal_fields:
            field_class = serializers.DecimalField
            field_kwargs = dict(
                max_digits=20,
                decimal_places=2,
                coerce_to_string=True,
            )
            default_kwargs = self.Meta.decimal_fields_extra_kwargs.get(field_name)
            if default_kwargs:
                field_kwargs.update(default_kwargs)
            return field_class, field_kwargs

        return super(InvoiceItemReportSerializer, self).build_field(field_name, info, model_class, nested_depth)

    def get_extra_kwargs(self):
        extra_kwargs = super(InvoiceItemReportSerializer, self).get_extra_kwargs()
        extra_kwargs.update(settings.INVOICES['INVOICE_REPORTING']['SERIALIZER_EXTRA_KWARGS'])
        return extra_kwargs


class OpenStackItemReportSerializer(InvoiceItemReportSerializer):
    class Meta(InvoiceItemReportSerializer.Meta):
        model = models.OpenStackItem


class OfferingItemReportSerializer(InvoiceItemReportSerializer):
    class Meta(InvoiceItemReportSerializer.Meta):
        model = models.OfferingItem


class GenericItemReportSerializer(InvoiceItemReportSerializer):
    class Meta(InvoiceItemReportSerializer.Meta):
        model = models.GenericInvoiceItem
