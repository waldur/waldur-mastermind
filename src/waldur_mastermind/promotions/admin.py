from django.contrib import admin

from . import models


class CampaignAdmin(admin.ModelAdmin):
    list_display = ('service_provider', 'start_date', 'end_date', 'state')


class DiscountedResourceAdmin(admin.ModelAdmin):
    list_display = (
        'resource',
        'created',
    )


admin.site.register(models.Campaign, CampaignAdmin)
admin.site.register(models.DiscountedResource, DiscountedResourceAdmin)
