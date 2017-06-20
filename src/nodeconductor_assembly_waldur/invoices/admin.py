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


class PaymentDetailsAdminForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(PaymentDetailsAdminForm, self).__init__(*args, **kwargs)
        self.fields['type'] = ChoiceField(choices=[(t, t) for t in settings.INVOICES['COMPANY_TYPES']])


class PaymentDetailsAdmin(admin.ModelAdmin):
    form = PaymentDetailsAdminForm


structure_admin.CustomerAdmin.inlines += [PaymentDetailsInline]
admin.site.register(models.Invoice, InvoiceAdmin)
admin.site.register(models.PaymentDetails, PaymentDetailsAdmin)
