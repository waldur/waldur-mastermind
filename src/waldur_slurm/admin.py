from django.contrib import admin
from django.utils.translation import ugettext_lazy as _

from waldur_core.structure import admin as structure_admin

from .models import SlurmService, SlurmServiceProjectLink, Allocation


def get_allocation_count(self, scope):
    return scope.quotas.get(name='nc_allocation_count').usage


get_allocation_count.short_description = _('Allocation count')

for cls in (structure_admin.CustomerAdmin, structure_admin.ProjectAdmin):
    cls.get_allocation_count = get_allocation_count
    cls.list_display += ('get_allocation_count',)

admin.site.register(SlurmService, structure_admin.ServiceAdmin)
admin.site.register(SlurmServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
admin.site.register(Allocation, structure_admin.ResourceAdmin)
