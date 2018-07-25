from __future__ import unicode_literals

from django import forms
from django.contrib import admin
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import admin as core_admin

from . import models


class PresetAdminForm(forms.ModelForm):
    class Meta:
        widgets = {
            'ram': core_admin.GBtoMBWidget(),
            'storage': core_admin.GBtoMBWidget(),
        }


class PresetAdmin(admin.ModelAdmin):
    def ram_gb(self, obj):
        return '%s GB' % core_admin.GBtoMBWidget().format_value(obj.ram)

    ram_gb.admin_order_field = 'ram'
    ram_gb.short_description = _('RAM')

    def storage_gb(self, obj):
        return '%s GB' % core_admin.GBtoMBWidget().format_value(obj.storage)

    storage_gb.admin_order_field = 'storage'
    storage_gb.short_description = _('Storage')

    form = PresetAdminForm
    base_model = models.Preset
    list_display = ('category', 'variant', 'name', 'ram_gb', 'cores', 'storage_gb')
    list_filter = ('category',)


class PresetInline(admin.TabularInline):
    form = PresetAdminForm
    model = models.Preset
    extra = 1


class CategoryAdmin(admin.ModelAdmin):
    inlines = (PresetInline,)


class DeploymentPlanItem(admin.TabularInline):
    model = models.DeploymentPlanItem
    extra = 1
    fields = ('preset', 'quantity')


class DeploymentPlanAdmin(admin.ModelAdmin):
    inlines = (DeploymentPlanItem,)
    search_fields = ('name',)
    list_display = ('name', 'project')


admin.site.register(models.Category, CategoryAdmin)
admin.site.register(models.Preset, PresetAdmin)
admin.site.register(models.DeploymentPlan, DeploymentPlanAdmin)
