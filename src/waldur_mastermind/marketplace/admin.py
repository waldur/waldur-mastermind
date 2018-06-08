from __future__ import unicode_literals

from django.contrib import admin

from . import models


class ServiceProviderAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'customer', 'created')


admin.site.register(models.ServiceProvider, ServiceProviderAdmin)
