from __future__ import unicode_literals

from django.contrib import admin

from . import models


class ExpertProviderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'customer', 'created')
    readonly_fields = ('customer', 'created')


admin.site.register(models.ExpertProvider, ExpertProviderAdmin)
