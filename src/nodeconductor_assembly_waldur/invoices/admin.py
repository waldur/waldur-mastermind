from django.contrib import admin

from nodeconductor.structure import admin as structure_admin

from . import models


class OpenStackItemInline(admin.TabularInline):
    model = models.OpenStackItem
    readonly_fields = ('name', 'package', 'package_details', 'price', 'start', 'end')

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


structure_admin.CustomerAdmin.inlines += [PaymentDetailsInline]
admin.site.register(models.Invoice, InvoiceAdmin)
admin.site.register(models.PaymentDetails)
admin.site.register(models.CompanyType)
