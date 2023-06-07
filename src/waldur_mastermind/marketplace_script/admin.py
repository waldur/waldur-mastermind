from django.contrib import admin

from waldur_core.core.admin import ReadOnlyAdminMixin

from . import models


class DryRunAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('order_item_offering', 'order_item_type', 'state')
    readonly_fields = (
        'order',
        'order_item_offering',
        'order_item_plan',
        'order_item_attributes',
        'order_item_type',
        'state',
        'output',
        'error_message',
        'error_traceback',
    )


admin.site.register(models.DryRun, DryRunAdmin)
