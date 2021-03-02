from django.conf.urls import url
from django.contrib import admin
from django.forms.models import ModelForm
from django.forms.widgets import CheckboxInput
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import ugettext_lazy as _
from rest_framework.reverse import reverse as rest_reverse
from reversion.admin import VersionAdmin

from waldur_core.core import admin as core_admin
from waldur_core.core.admin import JsonWidget

from . import models, tasks, utils


class GenericItemInline(core_admin.UpdateOnlyModelAdmin, admin.StackedInline):
    model = models.InvoiceItem
    readonly_fields = (
        'pk',
        'price',
        'unit_price',
        'unit',
        'project_name',
        'project_uuid',
        'get_factor',
        'quantity',
    )
    exclude = ('project', 'content_type', 'object_id')

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
    inlines = [GenericItemInline]
    fields = [
        'tax_percent',
        'invoice_date',
        'customer',
        'state',
        'total',
        'year',
        'month',
        'pdf_file',
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
            url(
                r'^(.+)/change/pdf_file/$',
                self.admin_site.admin_view(self.pdf_file_view),
            ),
        ]
        return my_urls + super(InvoiceAdmin, self).get_urls()

    def pdf_file_view(self, request, pk=None):
        invoice = models.Invoice.objects.get(id=pk)

        file = utils.create_invoice_pdf(invoice)
        file_response = HttpResponse(file, content_type='application/pdf')
        filename = invoice.get_filename()
        file_response[
            'Content-Disposition'
        ] = 'attachment; filename="{filename}"'.format(filename=filename)
        return file_response

    def pdf_file(self, obj):
        pdf_ref = rest_reverse('invoice-pdf', kwargs={'uuid': obj.uuid.hex},)

        return format_html('<a href="%s">download</a>' % pdf_ref)

    pdf_file.short_description = "File"

    def get_extra_actions(self):
        return [
            self.send_invoice_report,
            self.update_current_cost,
        ]

    def send_invoice_report(self, request):
        tasks.send_invoice_report.delay()
        message = _('Invoice report task has been scheduled')
        self.message_user(request, message)
        return redirect(reverse('admin:invoices_invoice_changelist'))

    send_invoice_report.short_description = _('Send invoice report as CSV to email')

    def update_current_cost(self, request):
        tasks.update_invoices_current_cost.delay()
        message = _('Task has been scheduled.')
        self.message_user(request, message)
        return redirect(reverse('admin:invoices_invoice_changelist'))

    send_invoice_report.short_description = _('Update current cost for invoices')


class ServiceDowntimeAdmin(admin.ModelAdmin):
    list_display = (
        'get_customer',
        'get_project',
        'offering',
        'resource',
        'start',
        'end',
    )
    list_display_links = ('get_customer',)
    search_fields = ('offering__name', 'resource__name')
    date_hierarchy = 'start'

    def get_readonly_fields(self, request, obj=None):
        # Downtime record is protected from modifications
        if obj is not None:
            return self.readonly_fields + ('start', 'end', 'offering', 'resource')
        return self.readonly_fields

    def get_customer(self, downtime):
        if downtime.offering:
            return downtime.offering.customer

        if downtime.resource:
            return downtime.resource.customer

    get_customer.short_description = _('Organization')

    def get_project(self, downtime):
        if downtime.resource:
            return downtime.resource.project

    get_project.short_description = _('Project')


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
admin.site.register(models.ServiceDowntime, ServiceDowntimeAdmin)
admin.site.register(models.PaymentProfile, PaymentProfileAdmin)
admin.site.register(models.Payment, PaymentAdmin)
