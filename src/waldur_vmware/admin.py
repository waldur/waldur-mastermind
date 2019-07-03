from __future__ import unicode_literals

from django.contrib import admin
from django.contrib.admin import options
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext_lazy as _

from waldur_core.core.admin import ExecutorAdminAction
from waldur_core.structure import admin as structure_admin

from . import executors, models


class DiskAdmin(structure_admin.ResourceAdmin):

    class Pull(ExecutorAdminAction):
        executor = executors.DiskPullExecutor
        short_description = _('Pull')

        def validate(self, instance):
            if instance.state not in (models.Disk.States.OK, models.Disk.States.ERRED):
                raise ValidationError(_('Disk has to be in OK or ERRED state.'))

    pull = Pull()


class VirtualMachineAdmin(structure_admin.ResourceAdmin):

    class Pull(ExecutorAdminAction):
        executor = executors.VirtualMachinePullExecutor
        short_description = _('Pull')

        def validate(self, instance):
            if instance.state not in (models.VirtualMachine.States.OK, models.VirtualMachine.States.ERRED):
                raise ValidationError(_('Virtual machine has to be in OK or ERRED state.'))

    pull = Pull()


class CustomerClusterInline(options.TabularInline):
    model = models.CustomerCluster
    extra = 1
    verbose_name_plural = 'Customer VMware clusters'


class CustomerNetworkInline(options.TabularInline):
    model = models.CustomerNetwork
    extra = 1
    verbose_name_plural = 'Customer VMware networks'


admin.site.register(models.VMwareService, structure_admin.ServiceAdmin)
admin.site.register(models.VMwareServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
admin.site.register(models.Disk, DiskAdmin)
admin.site.register(models.VirtualMachine, VirtualMachineAdmin)

structure_admin.CustomerAdmin.inlines += [CustomerClusterInline]
structure_admin.CustomerAdmin.inlines += [CustomerNetworkInline]
