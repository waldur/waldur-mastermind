from django.contrib import admin

from waldur_core.core.admin import ReadOnlyAdminMixin

from . import models


class DryRunAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('order_offering', 'order_type', 'state')
    readonly_fields = (
        'order_offering',
        'order_plan',
        'order_attributes',
        'order_type',
        'state',
        'output',
        'error_message',
        'error_traceback',
    )


admin.site.register(models.DryRun, DryRunAdmin)
