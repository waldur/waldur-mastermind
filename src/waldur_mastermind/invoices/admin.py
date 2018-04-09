from __future__ import unicode_literals

from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import admin as core_admin

from . import models, tasks


class InvoiceItemInline(core_admin.UpdateOnlyModelAdmin, admin.TabularInline):
    model = models.InvoiceItem
    readonly_fields = ('name', 'price', 'unit_price', 'unit', 'start', 'end',
                       'project_name', 'project_uuid', 'product_code', 'article_code')
    exclude = ('project',)


class OpenStackItemInline(InvoiceItemInline):
    model = models.OpenStackItem
    readonly_fields = InvoiceItemInline.readonly_fields + ('package', 'package_details')


class OfferingItemInline(InvoiceItemInline):
    model = models.OfferingItem
    readonly_fields = InvoiceItemInline.readonly_fields + ('offering', 'offering_details')


class InvoiceAdmin(core_admin.ExtraActionsMixin,
                   core_admin.UpdateOnlyModelAdmin,
                   admin.ModelAdmin):
    inlines = [OpenStackItemInline, OfferingItemInline]
    readonly_fields = ('customer', 'state', 'total', 'year', 'month')
    list_display = ('customer', 'total', 'year', 'month', 'state')
    list_filter = ('state', 'customer')
    search_fields = ('customer', 'uuid')

    def get_extra_actions(self):
        return [self.send_invoice_report]

    def send_invoice_report(self, request):
        tasks.send_invoice_report.delay()
        message = _('Invoice report task has been scheduled')
        self.message_user(request, message)
        return redirect(reverse('admin:invoices_invoice_changelist'))

    send_invoice_report.short_description = _('Send invoice report as CSV to email')


admin.site.register(models.Invoice, InvoiceAdmin)
