import pytz
from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms import ModelForm
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from waldur_core.core.admin import (
    ExecutorAdminAction,
    PasswordWidget,
    format_json_field,
)
from waldur_core.structure import admin as structure_admin

from . import executors, models


def _get_obj_admin_url(obj):
    return reverse(
        f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change",
        args=[obj.id],
    )


class TenantAdminForm(ModelForm):
    class Meta:
        widgets = {
            "user_password": PasswordWidget(),
        }


class TenantAdmin(structure_admin.ResourceAdmin):
    actions = (
        "pull",
        "detect_external_networks",
        "allocate_floating_ip",
        "pull_security_groups",
        "pull_server_groups",
        "pull_floating_ips",
        "pull_quotas",
    )
    form = TenantAdminForm

    class OKTenantAction(ExecutorAdminAction):
        """Execute action with tenant that is in state OK"""

        def validate(self, tenant):
            if tenant.state != models.Tenant.States.OK:
                raise ValidationError(
                    _("Tenant has to be in state OK to pull security groups.")
                )

    class PullSecurityGroups(OKTenantAction):
        executor = executors.TenantPullSecurityGroupsExecutor
        short_description = _("Pull security groups")

    pull_security_groups = PullSecurityGroups()

    class PullServerGroups(OKTenantAction):
        executor = executors.TenantPullServerGroupsExecutor
        short_description = _("Pull server groups")

    pull_server_groups = PullServerGroups()

    class AllocateFloatingIP(OKTenantAction):
        executor = executors.TenantAllocateFloatingIPExecutor
        short_description = _("Allocate floating IPs")

        def validate(self, tenant):
            super(TenantAdmin.AllocateFloatingIP, self).validate(tenant)
            if not tenant.external_network_id:
                raise ValidationError(
                    _("Tenant has to have external network to allocate floating IP.")
                )

    allocate_floating_ip = AllocateFloatingIP()

    class DetectExternalNetworks(OKTenantAction):
        executor = executors.TenantDetectExternalNetworkExecutor
        short_description = _(
            "Attempt to lookup and set external network id of the connected router"
        )

    detect_external_networks = DetectExternalNetworks()

    class PullFloatingIPs(OKTenantAction):
        executor = executors.TenantPullFloatingIPsExecutor
        short_description = _("Pull floating IPs")

    pull_floating_ips = PullFloatingIPs()

    class PullQuotas(OKTenantAction):
        executor = executors.TenantPullQuotasExecutor
        short_description = _("Pull quotas")

    pull_quotas = PullQuotas()

    class Pull(ExecutorAdminAction):
        executor = executors.TenantPullExecutor
        short_description = _("Pull")

        def validate(self, tenant):
            if tenant.state not in (
                models.Tenant.States.OK,
                models.Tenant.States.ERRED,
            ):
                raise ValidationError(_("Tenant has to be OK or erred."))
            if not tenant.backend_id:
                raise ValidationError(_("Tenant does not have backend ID."))

    pull = Pull()


class FlavorAdmin(structure_admin.BackendModelAdmin):
    list_display = ("name", "settings", "cores", "ram", "disk")


class ImageAdmin(structure_admin.BackendModelAdmin):
    list_display = (
        "name",
        "settings",
        "min_disk",
        "min_ram",
    )


class TenantResourceAdmin(structure_admin.ResourceAdmin):
    """Admin model for resources that are connected to tenant.

    Expects that resource has attribute `tenant`.
    """

    list_display = structure_admin.ResourceAdmin.list_display + ("get_tenant",)
    list_filter = structure_admin.ResourceAdmin.list_filter + ("tenant",)

    def get_tenant(self, obj):
        tenant = obj.tenant
        return f'<a href="{_get_obj_admin_url(tenant)}">{tenant.name}</a>'

    get_tenant.short_description = _("Tenant")
    get_tenant.allow_tags = True


class NetworkAdmin(structure_admin.ResourceAdmin):
    list_display_links = None
    list_display = ("is_external", "type") + structure_admin.ResourceAdmin.list_display
    fields = ("name", "tenant", "is_external", "type", "segmentation_id", "state")


class SubNetAdmin(structure_admin.ResourceAdmin):
    list_display_links = None
    list_display = (
        "network",
        "gateway_ip",
    ) + structure_admin.ResourceAdmin.list_display

    fields = (
        "name",
        "network",
        "cidr",
        "gateway_ip",
        "allocation_pools",
        "ip_version",
        "enable_dhcp",
        "dns_nameservers",
        "state",
    )


class CustomerOpenStackInline(admin.StackedInline):
    model = models.CustomerOpenStack
    classes = ["collapse"]
    extra = 1


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
                tenants=self.instance.tenant
            )
            self.fields["instance"].queryset = models.Instance.objects.filter(
                tenant=self.instance.tenant,
                project=self.instance.project,
            )
            self.fields["source_snapshot"].queryset = models.Snapshot.objects.filter(
                tenant=self.instance.tenant,
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
                tenant=self.instance.tenant
            )


class InstanceAdmin(ActionDetailsMixin, structure_admin.VirtualMachineAdmin):
    actions = structure_admin.VirtualMachineAdmin.actions + ["pull"]
    exclude = ("action_details",)
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


admin.site.register(models.Network, NetworkAdmin)
admin.site.register(models.SubNet, SubNetAdmin)
admin.site.register(models.SecurityGroup, structure_admin.ResourceAdmin)
admin.site.register(models.ServerGroup, structure_admin.ResourceAdmin)

admin.site.register(models.Tenant, TenantAdmin)
admin.site.register(models.Flavor, FlavorAdmin)
admin.site.register(models.Image, ImageAdmin)
admin.site.register(models.VolumeType, structure_admin.ServicePropertyAdmin)
admin.site.register(models.FloatingIP, structure_admin.ResourceAdmin)

admin.site.register(models.Volume, VolumeAdmin)
admin.site.register(models.VolumeAvailabilityZone, structure_admin.ServicePropertyAdmin)
admin.site.register(models.Snapshot, SnapshotAdmin)
admin.site.register(
    models.InstanceAvailabilityZone,
    structure_admin.ServicePropertyAdmin,
)
admin.site.register(models.Instance, InstanceAdmin)
admin.site.register(models.Backup, BackupAdmin)
admin.site.register(models.BackupSchedule, BackupScheduleAdmin)
admin.site.register(models.SnapshotSchedule, SnapshotScheduleAdmin)

structure_admin.CustomerAdmin.inlines += [CustomerOpenStackInline]
