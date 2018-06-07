from __future__ import unicode_literals

from django import forms
from django.contrib import admin
from jsoneditor.forms import JSONEditor

from . import models


class ServiceProviderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'customer', 'created')


class CategoryForm(forms.ModelForm):
    class Meta:
        widgets = {
            'features': JSONEditor(),
        }


class CategoryAdmin(admin.ModelAdmin):
    form = CategoryForm


class OfferingForm(forms.ModelForm):
    class Meta:
        widgets = {
            'features': JSONEditor(),
        }


class OfferingAdmin(admin.ModelAdmin):
    form = OfferingForm


admin.site.register(models.ServiceProvider, ServiceProviderAdmin)
admin.site.register(models.Category, CategoryAdmin)
admin.site.register(models.Offering, OfferingAdmin)
