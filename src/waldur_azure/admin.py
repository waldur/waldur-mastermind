from __future__ import unicode_literals

from django.contrib import admin

from waldur_core.structure import admin as structure_admin
from .models import AzureService, AzureServiceProjectLink, VirtualMachine


admin.site.register(VirtualMachine, structure_admin.VirtualMachineAdmin)
admin.site.register(AzureService, structure_admin.ServiceAdmin)
admin.site.register(AzureServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
