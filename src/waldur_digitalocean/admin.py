from django.contrib import admin

from waldur_core.structure import admin as structure_admin

from .models import DigitalOceanService, DigitalOceanServiceProjectLink, Droplet


admin.site.register(Droplet, structure_admin.VirtualMachineAdmin)
admin.site.register(DigitalOceanService, structure_admin.ServiceAdmin)
admin.site.register(DigitalOceanServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
