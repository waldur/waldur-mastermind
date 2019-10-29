from django.contrib import admin

from waldur_core.structure import admin as structure_admin

from . import models


admin.site.register(models.RancherService, structure_admin.ServiceAdmin)
admin.site.register(models.RancherServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
admin.site.register(models.Cluster)
admin.site.register(models.Node)
