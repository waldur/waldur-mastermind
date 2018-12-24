from __future__ import unicode_literals

from django.contrib import admin
from django.forms import ModelForm
from waldur_core.core.admin import JsonWidget

from . import models


class ExpertProviderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'customer', 'created')


class ExpertRequestModel(ModelForm):
    class Meta:
        widgets = {
            'extra': JsonWidget(),
        }


class ExpertRequestAdmin(admin.ModelAdmin):
    list_display = ('name', 'project', 'state', 'created')
    readonly_fields = ('project', 'created')
    list_filter = ('state', 'created')
    search_fields = ('name', 'uuid')
    form = ExpertRequestModel


admin.site.register(models.ExpertProvider, ExpertProviderAdmin)
admin.site.register(models.ExpertRequest, ExpertRequestAdmin)
