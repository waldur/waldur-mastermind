import pytz
from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from waldur_core.core.admin import ExecutorAdminAction, format_json_field
from waldur_core.structure import admin as structure_admin

from . import executors, models


class FlavorAdmin(structure_admin.BackendModelAdmin):
    list_display = ("name", "settings", "cores", "ram", "disk")


class ImageAdmin(structure_admin.BackendModelAdmin):
    list_display = ("name", "settings", "min_disk", "min_ram")


class FloatingIPAdmin(structure_admin.BackendModelAdmin):
    list_display = (
        "name",
        "address",
        "settings",
        "runtime_state",
        "backend_network_id",
        "is_booked",
    )


class SecurityGroupRule(admin.TabularInline):
    model = models.SecurityGroupRule
    fields = (
        "ethertype",
        "direction",
        "protocol",
        "from_port",
        "to_port",
        "cidr",
        "backend_id",
        "description",
        "remote_group",
    )
    fk_name = "security_group"
    readonly_fields = fields
    extra = 0
    can_delete = False


class SecurityGroupAdmin(structure_admin.BackendModelAdmin):
    inlines = [SecurityGroupRule]
    list_display = ("name", "settings")


class ServerGroupAdmin(structure_admin.BackendModelAdmin):
    list_display = ("name", "policy", "settings")


class MetadataMixin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return super().get_readonly_fields(request, obj) + ("format_metadata",)

    def format_metadata(self, obj):
        return format_json_field(obj.metadata)

    format_metadata.allow_tags = True
    format_metadata.short_description = _("Metadata")


class ImageMetadataMixin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return super().get_readonly_fields(request, obj) + ("format_image_metadata",)

    def format_image_metadata(self, obj):
        return format_json_field(obj.image_metadata)

    format_image_metadata.allow_tags = True
    format_image_metadata.short_description = _("Image metadata")


class ActionDetailsMixin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return super().get_readonly_fields(request, obj) + ("format_action_details",)

    def format_action_details(self, obj):
        return format_json_field(obj.action_details)

    format_action_details.allow_tags = True
    format_action_details.short_description = _("Action details")


class VolumeChangeForm(forms.ModelForm):
    class Meta:
        model = models.Volume
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["type"].queryset = models.VolumeType.objects.filter(
                settings=self.instance.service_settings,
            )
            self.fields["instance"].queryset = models.Instance.objects.filter(
                service_settings=self.instance.service_settings,
                project=self.instance.project,
            )
            self.fields["source_snapshot"].queryset = models.Snapshot.objects.filter(
                service_settings=self.instance.service_settings,
                project=self.instance.project,
            )


class VolumeAdmin(
    MetadataMixin, ImageMetadataMixin, ActionDetailsMixin, structure_admin.ResourceAdmin
):
    exclude = ("metadata", "image_metadata", "action_details")

    form = VolumeChangeForm

    class Pull(ExecutorAdminAction):
        executor = executors.VolumePullExecutor
        short_description = _("Pull")

        def validate(self, instance):
            if instance.state not in (
                models.Volume.States.OK,
                models.Volume.States.ERRED,
            ):
                raise ValidationError(_("Volume has to be in OK or ERRED state."))

    pull = Pull()


class SnapshotAdmin(structure_admin.ResourceAdmin):
    class Pull(ExecutorAdminAction):
        executor = executors.SnapshotPullExecutor
        short_description = _("Pull")

        def validate(self, instance):
            if instance.state not in (
                models.Snapshot.States.OK,
                models.Snapshot.States.ERRED,
            ):
                raise ValidationError(_("Snapshot has to be in OK or ERRED state."))

    pull = Pull()


class InternalIpInline(admin.TabularInline):
    model = models.InternalIP

    def has_add_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return models.InternalIP.get_backend_fields() + (
            "backend_id",
            "instance",
            "subnet",
        )


class InstanceChangeForm(forms.ModelForm):
    class Meta:
        model = models.Instance
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields[
                "security_groups"
            ].queryset = models.SecurityGroup.objects.filter(
                settings=self.instance.service_settings,
            )


class InstanceAdmin(ActionDetailsMixin, structure_admin.VirtualMachineAdmin):
    actions = structure_admin.VirtualMachineAdmin.actions + ["pull"]
    exclude = ("action_details",)
    inlines = [InternalIpInline]
    list_filter = structure_admin.VirtualMachineAdmin.list_filter + ("runtime_state",)
    search_fields = structure_admin.VirtualMachineAdmin.search_fields + (
        "uuid",
        "backend_id",
        "runtime_state",
    )
    form = InstanceChangeForm

    class Pull(ExecutorAdminAction):
        executor = executors.InstancePullExecutor
        short_description = _("Pull")

        def validate(self, instance):
            if instance.state not in (
                models.Instance.States.OK,
                models.Instance.States.ERRED,
            ):
                raise ValidationError(_("Instance has to be in OK or ERRED state."))

    pull = Pull()


class BackupAdmin(MetadataMixin, admin.ModelAdmin):
    readonly_fields = ("created", "kept_until")
    list_filter = ("state", "instance")
    list_display = ("uuid", "name", "instance", "state", "project")
    exclude = ("metadata",)

    def project(self, obj):
        return obj.instance.project

    project.short_description = _("Project")


class BaseScheduleForm(forms.ModelForm):
    def clean_timezone(self):
        tz = self.cleaned_data["timezone"]
        if tz not in pytz.all_timezones:
            raise ValidationError(_("Invalid timezone"), code="invalid")

        return self.cleaned_data["timezone"]


class BaseScheduleAdmin(structure_admin.ResourceAdmin):
    form = BaseScheduleForm
    readonly_fields = ("next_trigger_at",)
    list_filter = ("is_active",) + structure_admin.ResourceAdmin.list_filter
    list_display = (
        "uuid",
        "next_trigger_at",
        "is_active",
        "timezone",
    ) + structure_admin.ResourceAdmin.list_display


class BackupScheduleAdmin(BaseScheduleAdmin):
    list_display = BaseScheduleAdmin.list_display + ("instance",)
    list_filter = ("instance",) + BaseScheduleAdmin.list_filter


class SnapshotScheduleAdmin(BaseScheduleAdmin):
    list_display = BaseScheduleAdmin.list_display + ("source_volume",)


admin.site.register(models.Flavor, FlavorAdmin)
admin.site.register(models.Image, ImageAdmin)
admin.site.register(models.FloatingIP, FloatingIPAdmin)
admin.site.register(models.SecurityGroup, SecurityGroupAdmin)
admin.site.register(models.ServerGroup, ServerGroupAdmin)
admin.site.register(models.Volume, VolumeAdmin)
admin.site.register(models.VolumeType, structure_admin.ServicePropertyAdmin)
admin.site.register(models.VolumeAvailabilityZone, structure_admin.ServicePropertyAdmin)
admin.site.register(models.Snapshot, SnapshotAdmin)
admin.site.register(
    models.InstanceAvailabilityZone, structure_admin.ServicePropertyAdmin
)
admin.site.register(models.Instance, InstanceAdmin)
admin.site.register(models.Backup, BackupAdmin)
admin.site.register(models.BackupSchedule, BackupScheduleAdmin)
admin.site.register(models.SnapshotSchedule, SnapshotScheduleAdmin)
