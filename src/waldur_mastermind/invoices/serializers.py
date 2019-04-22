from __future__ import unicode_literals

import datetime
from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import serializers as core_serializers
from waldur_core.core import utils as core_utils
from waldur_mastermind.common.utils import quantize_price

from . import models


class InvoiceItemSerializer(serializers.HyperlinkedModelSerializer):
    tax = serializers.DecimalField(max_digits=15, decimal_places=7)
    total = serializers.DecimalField(max_digits=15, decimal_places=7)
    factor = serializers.ReadOnlyField(source='get_factor')

    scope_type = serializers.SerializerMethodField()
    scope_uuid = serializers.SerializerMethodField()

    class Meta(object):
        model = models.InvoiceItem
        fields = ('name', 'price', 'tax', 'total', 'unit_price', 'unit', 'factor',
                  'start', 'end', 'product_code', 'article_code', 'project_name', 'project_uuid',
                  'scope_type', 'scope_uuid',)

    def get_scope_type(self, item):
        # It should be implemented by inherited class
        return

    def get_scope_uuid(self, item):
        # It should be implemented by inherited class
        return


class GenericItemSerializer(InvoiceItemSerializer):
    details = serializers.JSONField()

    class Meta(InvoiceItemSerializer.Meta):
        model = models.GenericInvoiceItem
        fields = InvoiceItemSerializer.Meta.fields + ('quantity', 'details', 'usage_days',)

    def get_scope_type(self, item):
        try:
            return item.content_type.model_class().get_scope_type()
        except AttributeError:
            return None

    def get_scope_uuid(self, item):
        if item.scope:
            return item.scope.uuid.hex
        return item.details.get('scope_uuid')


class InvoiceSerializer(core_serializers.RestrictedSerializerMixin,
                        serializers.HyperlinkedModelSerializer):
    price = serializers.DecimalField(max_digits=15, decimal_places=7)
    tax = serializers.DecimalField(max_digits=15, decimal_places=7)
    total = serializers.DecimalField(max_digits=15, decimal_places=7)
    items = GenericItemSerializer(many=True)
    issuer_details = serializers.SerializerMethodField()
    customer_details = serializers.SerializerMethodField()
    due_date = serializers.DateField()
    file = serializers.SerializerMethodField()

    class Meta(object):
        model = models.Invoice
        fields = (
            'url', 'uuid', 'number', 'customer', 'price', 'tax', 'total',
            'state', 'year', 'month', 'issuer_details', 'invoice_date', 'due_date',
            'customer', 'customer_details', 'items', 'file',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
        }

    def get_issuer_details(self, invoice):
        return settings.WALDUR_INVOICES['ISSUER_DETAILS']

    def get_customer_details(self, invoice):
        return {
            'name': invoice.customer.name,
            'address': invoice.customer.address,
            'country': invoice.customer.country,
            'country_name': invoice.customer.get_country_display(),
            'email': invoice.customer.email,
            'postal': invoice.customer.postal,
            'phone_number': invoice.customer.phone_number,
            'bank_name': invoice.customer.bank_name,
            'bank_account': invoice.customer.bank_account,
        }

    def get_file(self, obj):
        if not obj.has_file():
            return None

        return reverse('invoice-pdf',
                       kwargs={'uuid': obj.uuid},
                       request=self.context['request'])


class InvoiceNotificationSerializer(serializers.Serializer):
    link_template = serializers.URLField(help_text=_('The template must include {uuid} parameter '
                                                     'e.g. http://example.com/invoice/{uuid}'))

    def validate_link_template(self, link_template):
        if '{uuid}' not in link_template:
            raise serializers.ValidationError(_("Link template must include '{uuid}' parameter."))

        return link_template


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
        model = models.GenericInvoiceItem
        fields = (
            'customer_uuid', 'customer_name',
            'project_uuid', 'project_name',
            'invoice_uuid', 'invoice_number',
            'invoice_year', 'invoice_month',
            'invoice_date', 'due_date',
            'invoice_price', 'invoice_tax', 'invoice_total',
            'name', 'article_code', 'product_code',
            'price', 'tax', 'total', 'unit_price', 'unit',
            'start', 'end',
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
        extra_kwargs.update(settings.WALDUR_INVOICES['INVOICE_REPORTING']['SERIALIZER_EXTRA_KWARGS'])
        return extra_kwargs


class GenericItemReportSerializer(InvoiceItemReportSerializer):
    class Meta(InvoiceItemReportSerializer.Meta):
        model = models.GenericInvoiceItem
        fields = InvoiceItemReportSerializer.Meta.fields + ('quantity',)


# SAF is accounting soft from Estonia: www.sysdec.ee/safsaf.htm
class SAFReportSerializer(serializers.Serializer):
    DOKNR = serializers.ReadOnlyField(source='invoice.number')
    KUUPAEV = serializers.SerializerMethodField(method_name='get_last_day_of_month')
    VORMKUUP = serializers.SerializerMethodField(method_name='get_invoice_date')
    MAKSEAEG = serializers.SerializerMethodField(method_name='get_due_date')
    YKSUS = serializers.ReadOnlyField(source='invoice.customer.agreement_number')
    PARTNER = serializers.ReadOnlyField(source='invoice.customer.agreement_number')
    ARTIKKEL = serializers.ReadOnlyField(source='article_code')
    KOGUS = serializers.SerializerMethodField(method_name='get_quantity')
    SUMMA = serializers.SerializerMethodField(method_name='get_total')
    RMAKSUSUM = serializers.SerializerMethodField(method_name='get_tax')
    RMAKSULIPP = serializers.SerializerMethodField(method_name='get_vat')
    ARTPROJEKT = serializers.SerializerMethodField(method_name='get_project')
    ARTNIMI = serializers.ReadOnlyField(source='name')
    VALI = serializers.SerializerMethodField(method_name='get_empty_field')
    U_KONEDEARV = serializers.SerializerMethodField(method_name='get_empty_field')
    H_PERIOOD = serializers.SerializerMethodField(method_name='get_covered_period')

    class Meta(object):
        fields = ('DOKNR', 'KUUPAEV', 'VORMKUUP', 'MAKSEAEG', 'YKSUS', 'PARTNER',
                  'ARTIKKEL', 'KOGUS', 'SUMMA', 'RMAKSUSUM', 'RMAKSULIPP',
                  'ARTPROJEKT', 'ARTNIMI', 'VALI', 'U_KONEDEARV', 'H_PERIOOD')

    def format_date(self, date):
        if date:
            return date.strftime('%d.%m.%Y')
        return ''

    def get_first_day(self, invoice_item):
        year = invoice_item.invoice.year
        month = invoice_item.invoice.month
        return datetime.date(year=year, month=month, day=1)

    def get_last_day_of_month(self, invoice_item):
        first_day = self.get_first_day(invoice_item)
        last_day = core_utils.month_end(first_day)
        return self.format_date(last_day)

    def get_invoice_date(self, invoice_item):
        date = invoice_item.invoice.invoice_date
        return self.format_date(date)

    def get_due_date(self, invoice_item):
        date = invoice_item.invoice.due_date
        return self.format_date(date)

    def get_quantity(self, invoice_item):
        if hasattr(invoice_item, 'quantity') and invoice_item.quantity:
            return invoice_item.quantity
        return invoice_item.usage_days

    def get_total(self, invoice_item):
        return quantize_price(invoice_item.price)

    def get_tax(self, invoice_item):
        return quantize_price(invoice_item.tax)

    def get_project(self, invoice_item):
        return settings.WALDUR_INVOICES['INVOICE_REPORTING']['SAF_PARAMS']['ARTPROJEKT']

    def get_vat(self, invoice_item):
        return settings.WALDUR_INVOICES['INVOICE_REPORTING']['SAF_PARAMS']['RMAKSULIPP']

    def get_empty_field(self, invoice_item):
        return ''

    def get_covered_period(self, invoice_item):
        first_day = self.get_first_day(invoice_item)
        last_day = core_utils.month_end(first_day)
        return '%s-%s' % (self.format_date(first_day), self.format_date(last_day))
