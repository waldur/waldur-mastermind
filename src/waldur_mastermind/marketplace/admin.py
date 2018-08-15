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


class PlansInline(admin.TabularInline):
    model = models.Plan


class OfferingAdmin(admin.ModelAdmin):
    inlines = [ScreenshotsInline, PlansInline]


class OrderItemInline(admin.TabularInline):
    model = models.OrderItem


class OrderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'project', 'created', 'state', 'total_cost')
    list_filter = ('state',)
    inlines = [OrderItemInline]


admin.site.register(models.ServiceProvider, ServiceProviderAdmin)
admin.site.register(models.Category)
admin.site.register(models.Offering, OfferingAdmin)
admin.site.register(models.Section)
admin.site.register(models.Attribute, AttributeAdmin)
admin.site.register(models.Screenshots)
admin.site.register(models.Order, OrderAdmin)
