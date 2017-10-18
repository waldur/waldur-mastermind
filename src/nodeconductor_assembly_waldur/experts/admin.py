from __future__ import unicode_literals

from django.contrib import admin
from django.forms import ModelForm
from jsoneditor.forms import JSONEditor

from . import models


class ExpertProviderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'customer', 'created')


class ExpertRequestModel(ModelForm):
    class Meta:
        widgets = {
            'extra': JSONEditor(),
        }


class ExpertRequestAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'project', 'state', 'created')
    readonly_fields = ('project', 'created')
    form = ExpertRequestModel


admin.site.register(models.ExpertProvider, ExpertProviderAdmin)
admin.site.register(models.ExpertRequest, ExpertRequestAdmin)
