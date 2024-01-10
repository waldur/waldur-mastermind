from django.contrib import admin
from django.forms.models import ModelForm
from django.forms.widgets import CheckboxInput
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from reversion.admin import VersionAdmin

from waldur_core.core import admin as core_admin
from waldur_core.core.admin import JsonWidget

from . import models, tasks


class InvoiceItemInline(core_admin.UpdateOnlyModelAdmin, admin.StackedInline):
    model = models.InvoiceItem
    fields = readonly_fields = (
        "name",
        "price",
        "unit_price",
        "unit",
        "measured_unit",
        "start",
        "end",
        "article_code",
        "project_name",
        "project_uuid",
        "quantity",
    )
    exclude = ("project",)

    def format_details(self, obj):
        return core_admin.format_json_field(obj.details)

    format_details.allow_tags = True
    format_details.short_description = _("Details")


class PaymentTypeFilter(admin.SimpleListFilter):
    title = _("Payment type")
    parameter_name = "payment_type"

    def lookups(self, request, model_admin):
        return models.PaymentType.CHOICES

    def queryset(self, request, queryset):
        payment_type = self.value()

        if payment_type:
            customer_ids = models.PaymentProfile.objects.filter(
                payment_type=payment_type, is_active=True
            ).values_list("id", flat=True)
            return queryset.filter(customer_id__in=customer_ids)
        else:
            return queryset


class InvoiceAdmin(
    core_admin.ExtraActionsMixin,
    core_admin.UpdateOnlyModelAdmin,
    VersionAdmin,
    admin.ModelAdmin,
):
    inlines = [InvoiceItemInline]
    fields = [
        "tax_percent",
        "invoice_date",
        "customer",
        "state",
        "total",
        "year",
        "month",
        "backend_id",
    ]
    readonly_fields = ("customer", "total", "year", "month")
    list_display = ("customer", "total", "year", "month", "state", "payment_type")
    list_filter = ("state", "customer", PaymentTypeFilter)
    search_fields = ("customer__name", "uuid")
    date_hierarchy = "invoice_date"

    def payment_type(self, obj):
        if obj.customer.paymentprofile_set.filter(is_active=True).exists():
            return obj.customer.paymentprofile_set.get(
                is_active=True
            ).get_payment_type_display()

        return ""

    payment_type.short_description = _("Payment type")

    def get_extra_actions(self):
        return [
            self.send_invoice_report,
            self.update_total_cost,
        ]

    def send_invoice_report(self, request):
        tasks.send_invoice_report.delay()
        message = _("Invoice report task has been scheduled")
        self.message_user(request, message)
        return redirect(reverse("admin:invoices_invoice_changelist"))

    send_invoice_report.short_description = _("Send invoice report as CSV to email")

    def update_total_cost(self, request):
        tasks.update_invoices_total_cost.delay()
        message = _("Task has been scheduled.")
        self.message_user(request, message)
        return redirect(reverse("admin:invoices_invoice_changelist"))

    send_invoice_report.short_description = _("Update current cost for invoices")


class PaymentProfileAdminForm(ModelForm):
    class Meta:
        widgets = {
            "attributes": JsonWidget(),
            "is_active": CheckboxInput(),
        }


class PaymentProfileAdmin(admin.ModelAdmin):
    form = PaymentProfileAdminForm
    list_display = ("organization", "payment_type", "is_active")
    search_fields = ("organization__name",)


class PaymentAdmin(admin.ModelAdmin):
    list_display = ("profile", "date_of_payment", "sum")


admin.site.register(models.Invoice, InvoiceAdmin)
admin.site.register(models.PaymentProfile, PaymentProfileAdmin)
admin.site.register(models.Payment, PaymentAdmin)
