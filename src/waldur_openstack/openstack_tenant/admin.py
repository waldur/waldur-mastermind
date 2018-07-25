from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext_lazy as _
import pytz

from waldur_core.core.admin import ExecutorAdminAction, format_json_field
from waldur_core.structure import admin as structure_admin

from . import executors, models


class FlavorAdmin(structure_admin.BackendModelAdmin):
    list_filter = ('settings',)
    list_display = ('name', 'settings', 'cores', 'ram', 'disk')


class ImageAdmin(structure_admin.BackendModelAdmin):
    list_filter = ('settings', )
    list_display = ('name', 'settings', 'min_disk', 'min_ram')


class FloatingIPAdmin(structure_admin.BackendModelAdmin):
    list_filter = ('settings',)
    list_display = ('address', 'settings', 'runtime_state', 'backend_network_id', 'is_booked')


class SecurityGroupRule(admin.TabularInline):
    model = models.SecurityGroupRule
    fields = ('protocol', 'from_port', 'to_port', 'cidr', 'backend_id')
    readonly_fields = fields
    extra = 0
    can_delete = False


class SecurityGroupAdmin(structure_admin.BackendModelAdmin):
    inlines = [SecurityGroupRule]
    list_filter = ('settings',)
    list_display = ('name', 'settings')


class MetadataMixin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return super(MetadataMixin, self).get_readonly_fields(request, obj) + ('format_metadata',)

    def format_metadata(self, obj):
        return format_json_field(obj.metadata)

    format_metadata.allow_tags = True
    format_metadata.short_description = _('Metadata')


class ImageMetadataMixin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return super(ImageMetadataMixin, self).get_readonly_fields(request, obj) + ('format_image_metadata',)

    def format_image_metadata(self, obj):
        return format_json_field(obj.image_metadata)

    format_image_metadata.allow_tags = True
    format_image_metadata.short_description = _('Image metadata')


class ActionDetailsMixin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return super(ActionDetailsMixin, self).get_readonly_fields(request, obj) + ('format_action_details',)

    def format_action_details(self, obj):
        return format_json_field(obj.action_details)

    format_action_details.allow_tags = True
    format_action_details.short_description = _('Action details')


class VolumeAdmin(MetadataMixin,
                  ImageMetadataMixin,
                  ActionDetailsMixin,
                  structure_admin.ResourceAdmin):

    exclude = ('metadata', 'image_metadata', 'action_details')

    class Pull(ExecutorAdminAction):
        executor = executors.VolumePullExecutor
        short_description = _('Pull')

        def validate(self, instance):
            if instance.state not in (models.Volume.States.OK, models.Volume.States.ERRED):
                raise ValidationError(_('Volume has to be in OK or ERRED state.'))

    pull = Pull()


class SnapshotAdmin(structure_admin.ResourceAdmin):

    class Pull(ExecutorAdminAction):
        executor = executors.SnapshotPullExecutor
        short_description = _('Pull')

        def validate(self, instance):
            if instance.state not in (models.Snapshot.States.OK, models.Snapshot.States.ERRED):
                raise ValidationError(_('Snapshot has to be in OK or ERRED state.'))

    pull = Pull()


class InternalIpInline(admin.TabularInline):
    model = models.InternalIP

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        return models.InternalIP.get_backend_fields() + ('backend_id', 'instance', 'subnet')


class InstanceAdmin(ActionDetailsMixin, structure_admin.VirtualMachineAdmin):
    actions = structure_admin.VirtualMachineAdmin.actions + ['pull']
    exclude = ('action_details',)
    inlines = [InternalIpInline]

    class Pull(ExecutorAdminAction):
        executor = executors.InstancePullExecutor
        short_description = _('Pull')

        def validate(self, instance):
            if instance.state not in (models.Instance.States.OK, models.Instance.States.ERRED):
                raise ValidationError(_('Instance has to be in OK or ERRED state.'))

    pull = Pull()


class BackupAdmin(MetadataMixin, admin.ModelAdmin):
    readonly_fields = ('created', 'kept_until')
    list_filter = ('uuid', 'state')
    list_display = ('uuid', 'instance', 'state', 'project')
    exclude = ('metadata',)

    def project(self, obj):
        return obj.instance.service_project_link.project

    project.short_description = _('Project')


class BaseScheduleForm(forms.ModelForm):
    def clean_timezone(self):
        tz = self.cleaned_data['timezone']
        if tz not in pytz.all_timezones:
            raise ValidationError(_('Invalid timezone'), code='invalid')

        return self.cleaned_data['timezone']


class BaseScheduleAdmin(structure_admin.ResourceAdmin):
    form = BaseScheduleForm
    readonly_fields = ('next_trigger_at',)
    list_filter = ('is_active',) + structure_admin.ResourceAdmin.list_filter
    list_display = ('uuid', 'next_trigger_at', 'is_active', 'timezone') + structure_admin.ResourceAdmin.list_display


class BackupScheduleAdmin(BaseScheduleAdmin):
    list_display = BaseScheduleAdmin.list_display + ('instance',)


class SnapshotScheduleAdmin(BaseScheduleAdmin):
    list_display = BaseScheduleAdmin.list_display + ('source_volume',)


admin.site.register(models.OpenStackTenantService, structure_admin.ServiceAdmin)
admin.site.register(models.OpenStackTenantServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
admin.site.register(models.Flavor, FlavorAdmin)
admin.site.register(models.Image, ImageAdmin)
admin.site.register(models.FloatingIP, FloatingIPAdmin)
admin.site.register(models.SecurityGroup, SecurityGroupAdmin)
admin.site.register(models.Volume, VolumeAdmin)
admin.site.register(models.Snapshot, SnapshotAdmin)
admin.site.register(models.Instance, InstanceAdmin)
admin.site.register(models.Backup, BackupAdmin)
admin.site.register(models.BackupSchedule, BackupScheduleAdmin)
admin.site.register(models.SnapshotSchedule, SnapshotScheduleAdmin)
