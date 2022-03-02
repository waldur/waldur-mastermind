from django.contrib import admin
from django.forms.models import ModelForm
from django.forms.widgets import CheckboxInput
from django.shortcuts import redirect
from django.urls import re_path, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from rest_framework.reverse import reverse as rest_reverse
from reversion.admin import VersionAdmin

from waldur_core.core import admin as core_admin
from waldur_core.core.admin import JsonWidget
from waldur_core.media.utils import format_pdf_response

from . import models, tasks, utils


class InvoiceItemInline(core_admin.UpdateOnlyModelAdmin, admin.StackedInline):
    model = models.InvoiceItem
    fields = readonly_fields = (
        'name',
        'price',
        'unit_price',
        'unit',
        'measured_unit',
        'start',
        'end',
        'article_code',
        'project_name',
        'project_uuid',
        'quantity',
    )
    exclude = ('project',)

    def format_details(self, obj):
        return core_admin.format_json_field(obj.details)

    format_details.allow_tags = True
    format_details.short_description = _('Details')


class PaymentTypeFilter(admin.SimpleListFilter):
    title = _('Payment type')
    parameter_name = 'payment_type'

    def lookups(self, request, model_admin):
        return models.PaymentType.CHOICES

    def queryset(self, request, queryset):
        payment_type = self.value()

        if payment_type:
            customer_ids = models.PaymentProfile.objects.filter(
                payment_type=payment_type, is_active=True
            ).values_list('id', flat=True)
            return queryset.filter(customer_id__in=customer_ids)
        else:
            return queryset


class InvoiceAdmin(
    VersionAdmin,
    core_admin.ExtraActionsMixin,
    core_admin.UpdateOnlyModelAdmin,
    admin.ModelAdmin,
):
    inlines = [InvoiceItemInline]
    fields = [
        'tax_percent',
        'invoice_date',
        'customer',
        'state',
        'total',
        'year',
        'month',
        'pdf_file',
        'backend_id',
    ]
    readonly_fields = ('customer', 'total', 'year', 'month', 'pdf_file')
    list_display = ('customer', 'total', 'year', 'month', 'state', 'payment_type')
    list_filter = ('state', 'customer', PaymentTypeFilter)
    search_fields = ('customer__name', 'uuid')
    date_hierarchy = 'invoice_date'
    actions = ('create_pdf',)

    def payment_type(self, obj):
        if obj.customer.paymentprofile_set.filter(is_active=True).exists():
            return obj.customer.paymentprofile_set.get(
                is_active=True
            ).get_payment_type_display()

        return ''

    payment_type.short_description = _('Payment type')

    def get_urls(self):
        my_urls = [
            re_path(
                r'^(.+)/change/pdf_file/$',
                self.admin_site.admin_view(self.pdf_file_view),
            ),
        ]
        return my_urls + super(InvoiceAdmin, self).get_urls()

    def pdf_file_view(self, request, pk=None):
        invoice = models.Invoice.objects.get(id=pk)

        file = utils.create_invoice_pdf(invoice)
        filename = invoice.get_filename()
        return format_pdf_response(file, filename)

    def pdf_file(self, obj):
        pdf_ref = rest_reverse(
            'invoice-pdf',
            kwargs={'uuid': obj.uuid.hex},
        )

        return format_html('<a href="%s">download</a>' % pdf_ref)

    pdf_file.short_description = "File"

    def get_extra_actions(self):
        return [
            self.send_invoice_report,
            self.update_total_cost,
        ]

    def send_invoice_report(self, request):
        tasks.send_invoice_report.delay()
        message = _('Invoice report task has been scheduled')
        self.message_user(request, message)
        return redirect(reverse('admin:invoices_invoice_changelist'))

    send_invoice_report.short_description = _('Send invoice report as CSV to email')

    def update_total_cost(self, request):
        tasks.update_invoices_total_cost.delay()
        message = _('Task has been scheduled.')
        self.message_user(request, message)
        return redirect(reverse('admin:invoices_invoice_changelist'))

    send_invoice_report.short_description = _('Update current cost for invoices')


class PaymentProfileAdminForm(ModelForm):
    class Meta:
        widgets = {
            'attributes': JsonWidget(),
            'is_active': CheckboxInput(),
        }


class PaymentProfileAdmin(admin.ModelAdmin):
    form = PaymentProfileAdminForm
    list_display = ('organization', 'payment_type', 'is_active')
    search_fields = ('organization__name',)


class PaymentAdmin(admin.ModelAdmin):
    list_display = ('profile', 'date_of_payment', 'sum')


admin.site.register(models.Invoice, InvoiceAdmin)
admin.site.register(models.PaymentProfile, PaymentProfileAdmin)
admin.site.register(models.Payment, PaymentAdmin)
