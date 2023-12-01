from django.contrib import admin

from . import models


class RequestedOfferingInline(admin.TabularInline):
    model = models.RequestedOffering
    extra = 1


class CallAdmin(admin.ModelAdmin):
    inlines = [RequestedOfferingInline]
    list_display = ('name', 'start_time', 'end_time')


admin.site.register(models.CallManagingOrganisation)
admin.site.register(models.Call, CallAdmin)
