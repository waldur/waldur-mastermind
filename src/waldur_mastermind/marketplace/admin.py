from __future__ import unicode_literals

from django import forms
from django.contrib import admin
from jsoneditor.forms import JSONEditor

from waldur_core.core.fields import JSONField

from . import models, attribute_types, utils


class ServiceProviderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'customer', 'created')


class OfferingForm(forms.ModelForm):
    class Meta:
        widgets = {
            'attributes': JSONEditor(),
        }


class OfferingAdmin(admin.ModelAdmin):
    form = OfferingForm


class AttributeForm(forms.ModelForm):
    def clean_available_values(self):
        value = self.cleaned_data['available_values']
        attribute_type = self.cleaned_data['type']
        klass_name = utils.snake_to_camel(attribute_type) + 'Attribute'
        klass = getattr(attribute_types, klass_name)
        klass.available_values_validate(JSONField().to_python(value))
        return value


class AttributeAdmin(admin.ModelAdmin):
    form = AttributeForm


admin.site.register(models.ServiceProvider, ServiceProviderAdmin)
admin.site.register(models.Category)
admin.site.register(models.Offering, OfferingAdmin)
admin.site.register(models.Section)
admin.site.register(models.Attribute, AttributeAdmin)
