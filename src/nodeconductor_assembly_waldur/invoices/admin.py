from django.contrib import admin

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


admin.site.register(models.Invoice, InvoiceAdmin)
