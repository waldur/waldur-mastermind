from __future__ import unicode_literals

from django.contrib import admin

from . import models


class ExpertProviderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'customer', 'created')


class ExpertRequestAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'project', 'state', 'created')
    readonly_fields = ('project', 'created')


admin.site.register(models.ExpertProvider, ExpertProviderAdmin)
admin.site.register(models.ExpertRequest, ExpertRequestAdmin)
