from django.contrib import admin

from waldur_core.structure import admin as structure_admin

from .models import VirtualMachine

admin.site.register(VirtualMachine, structure_admin.VirtualMachineAdmin)
