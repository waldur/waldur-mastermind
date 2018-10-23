from __future__ import unicode_literals

from django.contrib import admin
from django.forms import ModelForm, ModelChoiceField
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import admin as core_admin
from waldur_core.core.admin_filters import RelatedOnlyDropdownFilter
from waldur_mastermind.packages import models as package_models

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


class GenericItemInline(InvoiceItemInline):
    model = models.GenericInvoiceItem
    readonly_fields = InvoiceItemInline.readonly_fields + ('details', 'quantity')
    exclude = InvoiceItemInline.exclude + ('content_type', 'object_id')


class InvoiceAdmin(core_admin.ExtraActionsMixin,
                   core_admin.UpdateOnlyModelAdmin,
                   admin.ModelAdmin):
    inlines = [OpenStackItemInline, OfferingItemInline, GenericItemInline]
    readonly_fields = ('customer', 'state', 'total', 'year', 'month')
    list_display = ('customer', 'total', 'year', 'month', 'state')
    list_filter = ('state', 'customer')
    search_fields = ('customer', 'uuid')

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


class PackageChoiceField(ModelChoiceField):
    def label_from_instance(self, obj):
        return '%s > %s > %s' % (
            obj.tenant.service_project_link.project.customer,
            obj.tenant.service_project_link.project.name,
            obj.tenant.name
        )


class ServiceDowntimeForm(ModelForm):
    package = PackageChoiceField(
        queryset=package_models.OpenStackPackage.objects.order_by(
            'tenant__service_project_link__project__customer__name',
            'tenant__service_project_link__project__name',
            'tenant__name',
        )
    )


class ServiceDowntimeAdmin(admin.ModelAdmin):
    list_display = ('get_customer', 'get_project', 'get_name', 'start', 'end')
    list_display_links = ('get_name',)
    list_filter = (
        ('package__tenant__service_project_link__project__customer', RelatedOnlyDropdownFilter),
        ('package__tenant__service_project_link__project', RelatedOnlyDropdownFilter),
    )
    search_fields = ('package__tenant__name',)
    date_hierarchy = 'start'
    form = ServiceDowntimeForm

    def get_readonly_fields(self, request, obj=None):
        # Downtime record is protected from modifications
        if obj is not None:
            return self.readonly_fields + ('start', 'end', 'package')
        return self.readonly_fields

    def get_customer(self, downtime):
        return downtime.package.tenant.service_project_link.project.customer

    get_customer.short_description = _('Organization')
    get_customer.admin_order_field = 'package__tenant__service_project_link__project__customer'

    def get_project(self, downtime):
        return downtime.package.tenant.service_project_link.project

    get_project.short_description = _('Project')
    get_project.admin_order_field = 'package__tenant__service_project_link__project'

    def get_name(self, downtime):
        return downtime.package.tenant.name

    get_name.short_description = _('Resource')
    get_name.admin_order_field = 'package__tenant__name'


admin.site.register(models.Invoice, InvoiceAdmin)
admin.site.register(models.ServiceDowntime, ServiceDowntimeAdmin)
