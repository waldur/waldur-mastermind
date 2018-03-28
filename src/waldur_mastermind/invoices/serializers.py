from __future__ import unicode_literals

import datetime
from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core import utils as core_utils
from waldur_core.structure import SupportedServices
from waldur_mastermind.common.utils import quantize_price
from waldur_openstack.openstack import models as openstack_models
from waldur_mastermind.support import models as support_models

from . import models


class InvoiceItemSerializer(serializers.HyperlinkedModelSerializer):
    tax = serializers.DecimalField(max_digits=15, decimal_places=7)
    total = serializers.DecimalField(max_digits=15, decimal_places=7)
    factor = serializers.IntegerField(source='get_factor')

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


class OpenStackItemSerializer(InvoiceItemSerializer):
    tenant_name = serializers.ReadOnlyField(source='get_tenant_name')
    tenant_uuid = serializers.ReadOnlyField(source='get_tenant_uuid')
    template_name = serializers.ReadOnlyField(source='get_template_name')
    template_uuid = serializers.ReadOnlyField(source='get_template_uuid')
    template_category = serializers.ReadOnlyField(source='get_template_category')

    class Meta(InvoiceItemSerializer.Meta):
        model = models.OpenStackItem
        fields = InvoiceItemSerializer.Meta.fields + ('package', 'tenant_name', 'tenant_uuid', 'usage_days',
                                                      'template_name', 'template_uuid', 'template_category')
        extra_kwargs = {
            'package': {'lookup_field': 'uuid', 'view_name': 'openstack-package-detail'},
        }

    def get_scope_type(self, item):
        return SupportedServices.get_name_for_model(openstack_models.Tenant)

    def get_scope_uuid(self, item):
        return item.get_tenant_uuid()


class OfferingItemSerializer(InvoiceItemSerializer):
    offering_type = serializers.ReadOnlyField(source='get_offering_type')

    class Meta(InvoiceItemSerializer.Meta):
        model = models.OfferingItem
        fields = InvoiceItemSerializer.Meta.fields + ('offering', 'offering_type', 'usage_days')
        extra_kwargs = {
            'offering': {'lookup_field': 'uuid', 'view_name': 'support-offering-detail'},
        }

    def get_scope_type(self, item):
        return support_models.Offering.get_scope_type()

    def get_scope_uuid(self, item):
        return item.get_offering_uuid()


class GenericItemSerializer(InvoiceItemSerializer):

    class Meta(InvoiceItemSerializer.Meta):
        model = models.GenericInvoiceItem
        fields = InvoiceItemSerializer.Meta.fields + ('quantity',)

    def get_scope_type(self, item):
        return SupportedServices.get_name_for_model(item.content_type.model_class())

    def get_scope_uuid(self, item):
        if item.scope:
            return item.scope.uuid.hex
        return item.details['scope_uuid']


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
            'state', 'year', 'month', 'issuer_details', 'invoice_date', 'due_date',
            'customer', 'customer_details',
            'openstack_items', 'offering_items', 'generic_items',
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


class InvoiceNotificationSerializer(serializers.Serializer):
    link_template = serializers.URLField(help_text=_('The template must include {uuid} parameter '
                                                     'e.g. http://example.com/invoice/{uuid}'))

    def validate_link_template(self, link_template):
        if '{uuid}' not in link_template:
            raise serializers.ValidationError(_("Link template must include '{uuid}' parameter."))

        return link_template


class PaymentDetailsSerializer(core_serializers.AugmentedSerializerMixin,
                               serializers.HyperlinkedModelSerializer):

    type = serializers.ChoiceField(choices=[(t, t) for t in settings.WALDUR_CORE['COMPANY_TYPES']],
                                   allow_blank=True,
                                   required=False)
    email = serializers.EmailField(allow_blank=True, required=False, max_length=75)
    company = serializers.CharField(allow_blank=True, required=False, max_length=150)
    address = serializers.CharField(allow_blank=True, required=False, max_length=300)
    country = serializers.CharField(allow_blank=True, required=False, max_length=255)
    postal = serializers.CharField(allow_blank=True, required=False, max_length=20)
    phone = serializers.CharField(allow_blank=True, required=False, max_length=255)
    bank = serializers.CharField(allow_blank=True, required=False, max_length=150)
    account = serializers.CharField(allow_blank=True, required=False, max_length=50)
    default_tax_percent = serializers.DecimalField(max_digits=4, decimal_places=2, required=False)
    accounting_start_date = serializers.DateTimeField(required=False)

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


class OpenStackItemReportSerializer(InvoiceItemReportSerializer):
    class Meta(InvoiceItemReportSerializer.Meta):
        model = models.OpenStackItem
        fields = InvoiceItemReportSerializer.Meta.fields + ('usage_days',)


class OfferingItemReportSerializer(InvoiceItemReportSerializer):
    class Meta(InvoiceItemReportSerializer.Meta):
        model = models.OfferingItem
        fields = InvoiceItemReportSerializer.Meta.fields + ('usage_days',)


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
        if hasattr(invoice_item, 'quantity'):
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
