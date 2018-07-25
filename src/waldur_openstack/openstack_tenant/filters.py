import django_filters
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class OpenStackTenantServiceProjectLinkFilter(structure_filters.BaseServiceProjectLinkFilter):
    service = core_filters.URLFilter(view_name='openstacktenant-detail', name='service__uuid')

    class Meta(structure_filters.BaseServiceProjectLinkFilter.Meta):
        model = models.OpenStackTenantServiceProjectLink


class FlavorFilter(structure_filters.ServicePropertySettingsFilter):

    o = django_filters.OrderingFilter(fields=('cores', 'ram', 'disk'))

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Flavor
        fields = dict({
            'cores': ['exact', 'gte', 'lte'],
            'ram': ['exact', 'gte', 'lte'],
            'disk': ['exact', 'gte', 'lte'],
        }, **{field: ['exact'] for field in structure_filters.ServicePropertySettingsFilter.Meta.fields})


class NetworkFilter(structure_filters.ServicePropertySettingsFilter):

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Network
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + ('type', 'is_external')


class SubNetFilter(structure_filters.ServicePropertySettingsFilter):
    network = core_filters.URLFilter(view_name='openstacktenant-network-detail', name='network__uuid')
    network_uuid = django_filters.UUIDFilter(name='network__uuid')

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.SubNet
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + ('ip_version', 'enable_dhcp')


class FloatingIPFilter(structure_filters.ServicePropertySettingsFilter):
    free = django_filters.BooleanFilter(name='internal_ip', lookup_expr='isnull', widget=BooleanWidget)

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.FloatingIP
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + ('runtime_state', 'address', 'is_booked')


class VolumeFilter(structure_filters.BaseResourceFilter):
    instance = core_filters.URLFilter(view_name='openstacktenant-instance-detail', name='instance__uuid')
    instance_uuid = django_filters.UUIDFilter(name='instance__uuid')

    snapshot = core_filters.URLFilter(
        view_name='openstacktenant-snapshot-detail', name='restoration__snapshot__uuid')
    snapshot_uuid = django_filters.UUIDFilter(name='restoration__snapshot__uuid')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Volume
        fields = structure_filters.BaseResourceFilter.Meta.fields + ('runtime_state',)

    ORDERING_FIELDS = structure_filters.BaseResourceFilter.ORDERING_FIELDS + (
        ('instance__name', 'instance_name'),
        ('size', 'size'),
    )


class SnapshotFilter(structure_filters.BaseResourceFilter):
    source_volume_uuid = django_filters.UUIDFilter(name='source_volume__uuid')
    source_volume = core_filters.URLFilter(view_name='openstacktenant-volume-detail', name='source_volume__uuid')
    backup_uuid = django_filters.UUIDFilter(name='backups__uuid')
    backup = core_filters.URLFilter(view_name='openstacktenant-backup-detail', name='backups__uuid')

    snapshot_schedule = core_filters.URLFilter(
        view_name='openstacktenant-snapshot-schedule-detail', name='snapshot_schedule__uuid')
    snapshot_schedule_uuid = django_filters.UUIDFilter(name='snapshot_schedule__uuid')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Snapshot
        fields = structure_filters.BaseResourceFilter.Meta.fields + ('runtime_state',)

    ORDERING_FIELDS = structure_filters.BaseResourceFilter.ORDERING_FIELDS + (
        ('source_volume__name', 'source_volume_name'),
        ('size', 'size'),
    )


class InstanceFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Instance
        fields = structure_filters.BaseResourceFilter.Meta.fields + ('runtime_state',)

    ORDERING_FIELDS = structure_filters.BaseResourceFilter.ORDERING_FIELDS + (
        ('internal_ips_set__ip4_address', 'internal_ips'),
        ('internal_ips_set__floating_ips__address', 'external_ips'),
    )


class BackupFilter(structure_filters.BaseResourceFilter):
    instance = core_filters.URLFilter(view_name='openstacktenant-instance-detail', name='instance__uuid')
    instance_uuid = django_filters.UUIDFilter(name='instance__uuid')
    backup_schedule = core_filters.URLFilter(
        view_name='openstacktenant-backup-schedule-detail', name='backup_schedule__uuid')
    backup_schedule_uuid = django_filters.UUIDFilter(name='backup_schedule__uuid')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Backup


class BackupScheduleFilter(structure_filters.BaseResourceFilter):
    instance = core_filters.URLFilter(view_name='openstacktenant-instance-detail', name='instance__uuid')
    instance_uuid = django_filters.UUIDFilter(name='instance__uuid')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.BackupSchedule


class SnapshotScheduleFilter(structure_filters.BaseResourceFilter):
    source_volume = core_filters.URLFilter(view_name='openstacktenant-volume-detail', name='source_volume__uuid')
    source_volume_uuid = django_filters.UUIDFilter(name='source_volume__uuid')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.SnapshotSchedule


class SecurityGroupFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.SecurityGroup


class ImageFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Image
