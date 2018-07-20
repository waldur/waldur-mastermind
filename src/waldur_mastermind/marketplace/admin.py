from __future__ import unicode_literals

from django.contrib import admin

from . import models


class ServiceProviderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'customer', 'created')


class AttributeOptionInline(admin.TabularInline):
    model = models.AttributeOption


class AttributeAdmin(admin.ModelAdmin):
    inlines = [AttributeOptionInline]


class ScreenshotsInline(admin.TabularInline):
    model = models.Screenshots


class OfferingAdmin(admin.ModelAdmin):
    inlines = [ScreenshotsInline]


admin.site.register(models.ServiceProvider, ServiceProviderAdmin)
admin.site.register(models.Category)
admin.site.register(models.Offering, OfferingAdmin)
admin.site.register(models.Section)
admin.site.register(models.Attribute, AttributeAdmin)
admin.site.register(models.Screenshots)
