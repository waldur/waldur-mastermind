import logging

from django.contrib import admin
from django.utils.translation import ugettext_lazy as _

from waldur_core.core.admin import ExecutorAdminAction, UpdateOnlyModelAdmin
from waldur_core.structure import admin as structure_admin

from . import models, executors

logger = logging.getLogger(__name__)


class InvoiceItemInline(UpdateOnlyModelAdmin, admin.TabularInline):
    model = models.InvoiceItem
    readonly_fields = ('name', 'price', 'unit_price', 'unit_of_measure', 'start', 'end', 'tax', 'quantity')


class InvoiceAdmin(structure_admin.BackendModelAdmin):
    inlines = [InvoiceItemInline]
    list_display = ['customer', 'state', 'invoice_date', 'end_date', 'tax_percent', 'backend_id']
    actions = ['create_invoice', 'pull']

    class CreateInvoice(ExecutorAdminAction):
        executor = executors.InvoiceCreateExecutor
        short_description = _('Create invoice')

    create_invoice = CreateInvoice()

    class InvoicePull(ExecutorAdminAction):
        executor = executors.InvoicePullExecutor
        short_description = _('Pull invoice')

    pull = InvoicePull()


class PaymentAdmin(admin.ModelAdmin):
    list_display = ['customer', 'amount', 'state', 'backend_id']


admin.site.register(models.Invoice, InvoiceAdmin)
admin.site.register(models.Payment, PaymentAdmin)
