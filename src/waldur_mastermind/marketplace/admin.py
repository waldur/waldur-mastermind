from __future__ import unicode_literals

from django import forms
from django.contrib import admin
from jsoneditor.forms import JSONEditor

from waldur_core.core.fields import JSONField

from . import models, attribute_types


class ServiceProviderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'customer', 'created')


class AttributeForm(forms.ModelForm):
    def clean_available_values(self):
        value = self.cleaned_data['available_values']
        attribute_type = self.cleaned_data['type']
        klass = attribute_types.get_attribute_type(attribute_type)
        klass.available_values_validate(JSONField().to_python(value))
        return value

    class Meta:
        widgets = {
            'available_values': JSONEditor(),
        }


class AttributeAdmin(admin.ModelAdmin):
    form = AttributeForm


class ScreenshotsInline(admin.TabularInline):
    model = models.Screenshots


class OfferingAdmin(admin.ModelAdmin):
    inlines = [
        ScreenshotsInline,
    ]


admin.site.register(models.ServiceProvider, ServiceProviderAdmin)
admin.site.register(models.Category)
admin.site.register(models.Offering, OfferingAdmin)
admin.site.register(models.Section)
admin.site.register(models.Attribute, AttributeAdmin)
admin.site.register(models.Screenshots)
