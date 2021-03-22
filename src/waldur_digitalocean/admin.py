from django.contrib import admin

from waldur_core.structure import admin as structure_admin

from .models import Droplet

admin.site.register(Droplet, structure_admin.VirtualMachineAdmin)
