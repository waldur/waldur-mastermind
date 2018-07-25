from __future__ import unicode_literals

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext_lazy as _

from waldur_core.core.admin import ExecutorAdminAction
from waldur_core.structure import admin as structure_admin

from . import executors, models


class FlavorAdmin(structure_admin.BackendModelAdmin):
    list_filter = ('settings',)
    list_display = ('name', 'settings', 'cores', 'ram')


class FloatingIPAdmin(structure_admin.BackendModelAdmin):
    list_filter = ('settings',)
    list_display = ('address', 'settings', 'is_available')


class InternalIPAdmin(structure_admin.BackendModelAdmin):
    list_filter = ('settings',)
    list_display = ('address', 'settings', 'is_available')


class VolumeAdmin(structure_admin.ResourceAdmin):

    class Pull(ExecutorAdminAction):
        executor = executors.VolumePullExecutor
        short_description = _('Pull')

        def validate(self, instance):
            if instance.state not in (models.Volume.States.OK, models.Volume.States.ERRED):
                raise ValidationError(_('Volume has to be in OK or ERRED state.'))

    pull = Pull()


class InstanceAdmin(structure_admin.VirtualMachineAdmin):
    actions = structure_admin.VirtualMachineAdmin.actions + ['pull']

    class Pull(ExecutorAdminAction):
        executor = executors.InstancePullExecutor
        short_description = _('Pull')

        def validate(self, instance):
            if instance.state not in (models.Instance.States.OK, models.Instance.States.ERRED):
                raise ValidationError(_('Instance has to be in OK or ERRED state.'))

    pull = Pull()


admin.site.register(models.RijkscloudService, structure_admin.ServiceAdmin)
admin.site.register(models.RijkscloudServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
admin.site.register(models.Flavor, FlavorAdmin)
admin.site.register(models.FloatingIP, FloatingIPAdmin)
admin.site.register(models.InternalIP, InternalIPAdmin)
admin.site.register(models.Volume, VolumeAdmin)
admin.site.register(models.Instance, InstanceAdmin)
