from django.contrib import admin
from . import models


class SlurmPackageAdmin(admin.ModelAdmin):
    list_display = ('name', 'service_settings', 'cpu_price', 'gpu_price', 'ram_price')
    list_filter = ('service_settings',)


admin.site.register(models.SlurmPackage, SlurmPackageAdmin)
