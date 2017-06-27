from django.conf import settings
from django.contrib import admin
from django.forms import ModelForm, ChoiceField

from nodeconductor.structure import admin as structure_admin

from . import models


class OpenStackItemInline(admin.TabularInline):
    model = models.OpenStackItem
    readonly_fields = ('name', 'package', 'package_details', 'price', 'start', 'end',
                       'product_code', 'article_code')

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class InvoiceAdmin(admin.ModelAdmin):
    inlines = [OpenStackItemInline]
    readonly_fields = ('customer', 'state', 'total', 'year', 'month')
    list_display = ('customer', 'total', 'year', 'month', 'state')
    list_filter = ('state', 'customer')
    search_fields = ('customer', 'uuid')

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class PaymentDetailsInline(admin.StackedInline):
    model = models.PaymentDetails

    def get_readonly_fields(self, request, obj=None):
        fields = super(PaymentDetailsInline, self).get_readonly_fields(request, obj)
        if obj and hasattr(obj, 'payment_details') and obj.payment_details.is_billable():
            fields += ('accounting_start_date',)
        return fields


class PaymentDetailsAdminForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(PaymentDetailsAdminForm, self).__init__(*args, **kwargs)
        self.fields['type'] = ChoiceField(choices=[(t, t) for t in settings.INVOICES['COMPANY_TYPES']])


class PaymentDetailsAdmin(admin.ModelAdmin):
    form = PaymentDetailsAdminForm

    def get_readonly_fields(self, request, obj=None):
        fields = super(PaymentDetailsAdmin, self).get_readonly_fields(request, obj)
        if obj and obj.is_billable():
            fields += ('accounting_start_date',)
        return fields


structure_admin.CustomerAdmin.inlines += [PaymentDetailsInline]
admin.site.register(models.Invoice, InvoiceAdmin)
admin.site.register(models.PaymentDetails, PaymentDetailsAdmin)
