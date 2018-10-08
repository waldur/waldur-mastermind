from __future__ import unicode_literals

from django.contrib import admin
from django.forms.models import ModelForm
from django.utils.translation import ugettext_lazy as _

from jsoneditor.forms import JSONEditor

from . import models


class ServiceProviderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'customer', 'created')


class AttributeOptionInline(admin.TabularInline):
    model = models.AttributeOption


class AttributeAdmin(admin.ModelAdmin):
    inlines = [AttributeOptionInline]
    list_display = ('title', 'get_category', 'section', 'type', 'key', 'required',)
    list_filter = ('section',)
    ordering = ('section', 'title')

    def get_category(self, obj):
        return obj.section.category

    get_category.short_description = _('Category')
    get_category.admin_order_field = 'section__category__title'


class AttributeInline(admin.TabularInline):
    model = models.Attribute


class SectionAdmin(admin.ModelAdmin):
    inlines = [AttributeInline]
    list_display = ('title', 'category', 'key')


class SectionInline(admin.TabularInline):
    model = models.Section


class CategoryColumnInline(admin.TabularInline):
    model = models.CategoryColumn
    list_display = ('index', 'title', 'attribute', 'widget')


class CategoryAdmin(admin.ModelAdmin):
    inlines = [SectionInline, CategoryColumnInline]


class ScreenshotsInline(admin.TabularInline):
    model = models.Screenshot
    fields = ('name', 'description', 'image')


class PlansInline(admin.TabularInline):
    model = models.Plan
    fields = ('name', 'description', 'unit_price', 'unit',
              'product_code', 'article_code', 'archived')


class PlanComponentInline(admin.TabularInline):
    model = models.PlanComponent


class PlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'offering', 'archived', 'unit', 'unit_price')
    list_filter = ('offering', 'archived')
    search_fields = ('name', 'offering__name')
    inlines = [PlanComponentInline]


class OfferingAdminForm(ModelForm):
    class Meta:
        widgets = {
            'attributes': JSONEditor(),
            'geolocations': JSONEditor(),
            'options': JSONEditor(),
        }


class OfferingComponentInline(admin.TabularInline):
    model = models.OfferingComponent


class OfferingAdmin(admin.ModelAdmin):
    form = OfferingAdminForm
    inlines = [ScreenshotsInline, PlansInline, OfferingComponentInline]
    list_display = ('name', 'customer', 'state')
    list_filter = ('state',)
    fields = ('state', 'customer', 'category', 'name', 'native_name',
              'description', 'native_description', 'full_description',
              'rating', 'thumbnail', 'attributes', 'options', 'geolocations',
              'shared', 'allowed_customers', 'type', 'scope', 'vendor_details')
    readonly_fields = ('rating', 'scope')


class OrderItemInline(admin.TabularInline):
    model = models.OrderItem
    fields = ('offering', 'scope', 'state', 'attributes', 'cost', 'plan')
    readonly_fields = fields


class OrderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'project', 'created', 'created_by', 'state', 'total_cost')
    list_filter = ('state', 'created')
    ordering = ('-created',)
    inlines = [OrderItemInline]


admin.site.register(models.ServiceProvider, ServiceProviderAdmin)
admin.site.register(models.Category, CategoryAdmin)
admin.site.register(models.Offering, OfferingAdmin)
admin.site.register(models.Section, SectionAdmin)
admin.site.register(models.Attribute, AttributeAdmin)
admin.site.register(models.Screenshot)
admin.site.register(models.Order, OrderAdmin)
admin.site.register(models.Plan, PlanAdmin)
