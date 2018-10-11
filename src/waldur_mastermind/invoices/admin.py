from __future__ import unicode_literals

from django.contrib import admin
from django.forms import ModelForm, ModelChoiceField
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import admin as core_admin
from waldur_core.core.admin_filters import RelatedDropdownFilter
from waldur_core.structure import models as structure_models
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps

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


class ServiceSettingsChoiceField(ModelChoiceField):
    def label_from_instance(self, obj):
        return '%s - %s' % (obj.customer, obj.name)


class ServiceDowntimeForm(ModelForm):
    settings = ServiceSettingsChoiceField(
        queryset=structure_models.ServiceSettings.objects.filter(
            shared=False,
            type=openstack_tenant_apps.OpenStackTenantConfig.service_name
        ).order_by('customer__name', 'name'))


class ServiceDowntimeAdmin(admin.ModelAdmin):
    list_display = ('get_customer', 'get_settings', 'start', 'end')
    list_display_links = ('get_settings',)
    list_filter = (
        ('settings__customer', RelatedDropdownFilter),
        ('settings', RelatedDropdownFilter),
    )
    date_hierarchy = 'start'
    form = ServiceDowntimeForm

    def get_readonly_fields(self, request, obj=None):
        # Downtime record is protected from modifications
        if obj is not None:
            return self.readonly_fields + ('start', 'end', 'settings')
        return self.readonly_fields

    def get_customer(self, downtime):
        return downtime.settings.customer

    get_customer.short_description = _('Organization')
    get_customer.admin_order_field = 'settings__customer'

    def get_settings(self, downtime):
        return downtime.settings.name

    get_settings.short_description = _('Service settings')
    get_settings.admin_order_field = 'settings'


admin.site.register(models.Invoice, InvoiceAdmin)
admin.site.register(models.ServiceDowntime, ServiceDowntimeAdmin)
