from __future__ import unicode_literals


from django.core.validators import RegexValidator
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from six.moves.urllib.parse import urlparse

from waldur_core.core import models as core_models
from waldur_core.core.fields import JSONField
from waldur_core.logging.loggers import LoggableMixin
from waldur_core.quotas import models as quotas_models, fields as quotas_fields
from waldur_core.structure import models as structure_models, utils as structure_utils
from waldur_openstack.openstack_base import models as openstack_base_models
from waldur_openstack.openstack import models as openstack_models


TenantQuotas = openstack_models.Tenant.Quotas


class OpenStackTenantService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project, related_name='openstack_tenant_services', through='OpenStackTenantServiceProjectLink')

    class Meta:
        unique_together = ('customer', 'settings')
        verbose_name = _('OpenStackTenant provider')
        verbose_name_plural = _('OpenStackTenant providers')

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant'


class OpenStackTenantServiceProjectLink(structure_models.CloudServiceProjectLink):
    service = models.ForeignKey(OpenStackTenantService)

    class Meta(structure_models.CloudServiceProjectLink.Meta):
        verbose_name = _('OpenStackTenant provider project link')
        verbose_name_plural = _('OpenStackTenant provider project links')

    class Quotas(quotas_models.QuotaModelMixin.Quotas):
        vcpu = quotas_fields.TotalQuotaField(
            target_models=lambda: [Instance],
            path_to_scope='service_project_link',
            target_field='cores',
        )
        ram = quotas_fields.TotalQuotaField(
            target_models=lambda: [Instance],
            path_to_scope='service_project_link',
            target_field='ram',
        )
        storage = quotas_fields.TotalQuotaField(
            target_models=lambda: [Volume, Snapshot],
            path_to_scope='service_project_link',
            target_field='size',
        )

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-spl'


class Flavor(LoggableMixin, structure_models.ServiceProperty):
    cores = models.PositiveSmallIntegerField(help_text=_('Number of cores in a VM'))
    ram = models.PositiveIntegerField(help_text=_('Memory size in MiB'))
    disk = models.PositiveIntegerField(help_text=_('Root disk size in MiB'))

    class Meta(object):
        unique_together = ('settings', 'backend_id')

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-flavor'

    @classmethod
    def get_backend_fields(cls):
        return super(Flavor, cls).get_backend_fields() + ('cores', 'ram', 'disk')


class Image(openstack_base_models.BaseImage):
    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-image'


class SecurityGroup(core_models.DescribableMixin, structure_models.ServiceProperty):

    class Meta(object):
        unique_together = ('settings', 'backend_id')

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-sgp'


class SecurityGroupRule(openstack_base_models.BaseSecurityGroupRule):
    security_group = models.ForeignKey(SecurityGroup, related_name='rules')


class TenantQuotaMixin(quotas_models.SharedQuotaMixin):
    """
    It allows to update both service settings and shared tenant quotas.
    """

    def get_quota_scopes(self):
        service_settings = self.service_project_link.service.settings
        return service_settings, service_settings.scope


@python_2_unicode_compatible
class FloatingIP(structure_models.ServiceProperty):
    address = models.GenericIPAddressField(protocol='IPv4', null=True, default=None)
    runtime_state = models.CharField(max_length=30)
    backend_network_id = models.CharField(max_length=255, editable=False)
    is_booked = models.BooleanField(default=False,
                                    help_text=_('Marks if floating IP has been booked for provisioning.'))
    internal_ip = models.ForeignKey('InternalIP', related_name='floating_ips', null=True, on_delete=models.SET_NULL)

    class Meta:
        unique_together = ('settings', 'address')
        verbose_name = _('Floating IP')
        verbose_name_plural = _('Floating IPs')

    def __str__(self):
        return '%s:%s | %s' % (self.address, self.runtime_state, self.settings)

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-fip'

    def get_backend(self):
        return self.settings.get_backend()

    def increase_backend_quotas_usage(self, validate=True):
        self.settings.add_quota_usage(self.settings.Quotas.floating_ip_count, 1, validate=validate)

    def decrease_backend_quotas_usage(self):
        self.settings.add_quota_usage(self.settings.Quotas.floating_ip_count, -1)

    @classmethod
    def get_backend_fields(cls):
        return super(FloatingIP, cls).get_backend_fields() + ('address', 'runtime_state', 'backend_network_id')


class Volume(TenantQuotaMixin, structure_models.Volume):
    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(max_length=255, blank=True, null=True)

    service_project_link = models.ForeignKey(
        OpenStackTenantServiceProjectLink, related_name='volumes', on_delete=models.PROTECT)
    instance = models.ForeignKey('Instance', related_name='volumes', blank=True, null=True)
    device = models.CharField(
        max_length=50, blank=True,
        validators=[RegexValidator('^/dev/[a-zA-Z0-9]+$',
                                   message=_('Device should match pattern "/dev/alphanumeric+"'))],
        help_text=_('Name of volume as instance device e.g. /dev/vdb.'))
    bootable = models.BooleanField(default=False)
    metadata = JSONField(blank=True)
    image = models.ForeignKey(Image, blank=True, null=True, on_delete=models.SET_NULL)
    image_name = models.CharField(max_length=150, blank=True)
    image_metadata = JSONField(blank=True)
    type = models.CharField(max_length=100, blank=True)
    source_snapshot = models.ForeignKey('Snapshot', related_name='volumes', blank=True, null=True,
                                        on_delete=models.SET_NULL)
    # TODO: Move this fields to resource model.
    action = models.CharField(max_length=50, blank=True)
    action_details = JSONField(default=dict)

    tracker = FieldTracker()

    class Meta(object):
        unique_together = ('service_project_link', 'backend_id')

    def get_quota_deltas(self):
        return {
            TenantQuotas.volumes: 1,
            TenantQuotas.volumes_size: self.size,
            TenantQuotas.storage: self.size,
        }

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-volume'

    @classmethod
    def get_backend_fields(cls):
        return super(Volume, cls).get_backend_fields() + ('name', 'description', 'size', 'metadata', 'type', 'bootable',
                                                          'runtime_state', 'device')


class Snapshot(TenantQuotaMixin, structure_models.Snapshot):
    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(max_length=255, blank=True, null=True)

    service_project_link = models.ForeignKey(
        OpenStackTenantServiceProjectLink, related_name='snapshots', on_delete=models.PROTECT)
    source_volume = models.ForeignKey(Volume, related_name='snapshots', null=True, on_delete=models.PROTECT)
    metadata = JSONField(blank=True)
    # TODO: Move this fields to resource model.
    action = models.CharField(max_length=50, blank=True)
    action_details = JSONField(default=dict)
    snapshot_schedule = models.ForeignKey('SnapshotSchedule',
                                          blank=True,
                                          null=True,
                                          on_delete=models.SET_NULL,
                                          related_name='snapshots')

    tracker = FieldTracker()

    kept_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_('Guaranteed time of snapshot retention. If null - keep forever.'))

    class Meta(object):
        unique_together = ('service_project_link', 'backend_id')

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-snapshot'

    def get_quota_deltas(self):
        return {
            TenantQuotas.snapshots: 1,
            TenantQuotas.snapshots_size: self.size,
            TenantQuotas.storage: self.size,
        }

    @classmethod
    def get_backend_fields(cls):
        return super(Snapshot, cls).get_backend_fields() + ('name', 'description', 'size', 'metadata', 'source_volume',
                                                            'runtime_state')


class SnapshotRestoration(core_models.UuidMixin, TimeStampedModel):
    snapshot = models.ForeignKey(Snapshot, related_name='restorations')
    volume = models.OneToOneField(Volume, related_name='restoration')

    class Permissions(object):
        customer_path = 'snapshot__service_project_link__project__customer'
        project_path = 'snapshot__service_project_link__project'


class Instance(TenantQuotaMixin, structure_models.VirtualMachine):

    class RuntimeStates(object):
        # All possible OpenStack Instance states on backend.
        # See https://docs.openstack.org/developer/nova/vmstates.html
        ACTIVE = 'ACTIVE'
        BUILDING = 'BUILDING'
        DELETED = 'DELETED'
        SOFT_DELETED = 'SOFT_DELETED'
        ERROR = 'ERROR'
        UNKNOWN = 'UNKNOWN'
        HARD_REBOOT = 'HARD_REBOOT'
        REBOOT = 'REBOOT'
        REBUILD = 'REBUILD'
        PASSWORD = 'PASSWORD'
        PAUSED = 'PAUSED'
        RESCUED = 'RESCUED'
        RESIZED = 'RESIZED'
        REVERT_RESIZE = 'REVERT_RESIZE'
        SHUTOFF = 'SHUTOFF'
        STOPPED = 'STOPPED'
        SUSPENDED = 'SUSPENDED'
        VERIFY_RESIZE = 'VERIFY_RESIZE'

    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(max_length=255, blank=True, null=True)
    service_project_link = models.ForeignKey(
        OpenStackTenantServiceProjectLink, related_name='instances', on_delete=models.PROTECT)

    flavor_name = models.CharField(max_length=255, blank=True)
    flavor_disk = models.PositiveIntegerField(default=0, help_text=_('Flavor disk size in MiB'))
    security_groups = models.ManyToManyField(SecurityGroup, related_name='instances')
    # TODO: Move this fields to resource model.
    action = models.CharField(max_length=50, blank=True)
    action_details = JSONField(default=dict)
    subnets = models.ManyToManyField('SubNet', through='InternalIP')

    tracker = FieldTracker()

    class Meta(object):
        unique_together = ('service_project_link', 'backend_id')

    @property
    def external_ips(self):
        return list(self.floating_ips.values_list('address', flat=True))

    @property
    def internal_ips(self):
        return list(self.internal_ips_set.values_list('ip4_address', flat=True))

    @property
    def size(self):
        return self.volumes.aggregate(models.Sum('size'))['size__sum']

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-instance'

    def get_log_fields(self):
        return ('uuid', 'name', 'type', 'service_project_link', 'ram', 'cores',)

    def detect_coordinates(self):
        settings = self.service_project_link.service.settings
        options = settings.options or {}
        if 'latitude' in options and 'longitude' in options:
            return structure_utils.Coordinates(latitude=settings['latitude'], longitude=settings['longitude'])
        else:
            hostname = urlparse(settings.backend_url).hostname
            if hostname:
                return structure_utils.get_coordinates_by_ip(hostname)

    def get_quota_deltas(self):
        return {
            TenantQuotas.instances: 1,
            TenantQuotas.ram: self.ram,
            TenantQuotas.vcpu: self.cores,
        }

    @property
    def floating_ips(self):
        return FloatingIP.objects.filter(internal_ip__instance=self)

    @classmethod
    def get_backend_fields(cls):
        return super(Instance, cls).get_backend_fields() + ('flavor_name', 'flavor_disk', 'ram', 'cores', 'disk',
                                                            'runtime_state')

    @classmethod
    def get_online_state(cls):
        return Instance.RuntimeStates.ACTIVE

    @classmethod
    def get_offline_state(cls):
        return Instance.RuntimeStates.SHUTOFF


class Backup(structure_models.SubResource):
    service_project_link = models.ForeignKey(
        OpenStackTenantServiceProjectLink, related_name='backups', on_delete=models.PROTECT)
    instance = models.ForeignKey(Instance, related_name='backups', on_delete=models.PROTECT)
    backup_schedule = models.ForeignKey('BackupSchedule', blank=True, null=True,
                                        on_delete=models.SET_NULL,
                                        related_name='backups')
    kept_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_('Guaranteed time of backup retention. If null - keep forever.'))
    metadata = JSONField(
        blank=True,
        help_text=_('Additional information about backup, can be used for backup restoration or deletion'),
    )
    snapshots = models.ManyToManyField('Snapshot', related_name='backups')

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-backup'


class BackupRestoration(core_models.UuidMixin, TimeStampedModel):
    """ This model corresponds to instance restoration from backup. """
    backup = models.ForeignKey(Backup, related_name='restorations')
    instance = models.OneToOneField(Instance, related_name='+')
    flavor = models.ForeignKey(Flavor, related_name='+', null=True, blank=True, on_delete=models.SET_NULL)

    class Permissions(object):
        customer_path = 'backup__service_project_link__project__customer'
        project_path = 'backup__service_project_link__project'


class BaseSchedule(structure_models.NewResource, core_models.ScheduleMixin):
    retention_time = models.PositiveIntegerField(
        help_text=_('Retention time in days, if 0 - resource will be kept forever'))
    maximal_number_of_resources = models.PositiveSmallIntegerField()
    call_count = models.PositiveSmallIntegerField(default=0,
                                                  help_text=_('How many times a resource schedule was called.'))

    class Meta(object):
        abstract = True


class BackupSchedule(BaseSchedule):
    service_project_link = models.ForeignKey(
        OpenStackTenantServiceProjectLink, related_name='backup_schedules', on_delete=models.PROTECT)
    instance = models.ForeignKey(Instance, related_name='backup_schedules')

    tracker = FieldTracker()

    def __str__(self):
        return 'BackupSchedule of %s. Active: %s' % (self.instance, self.is_active)

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-backup-schedule'


class SnapshotSchedule(BaseSchedule):
    service_project_link = models.ForeignKey(
        OpenStackTenantServiceProjectLink, related_name='snapshot_schedules', on_delete=models.PROTECT)
    source_volume = models.ForeignKey(Volume, related_name='snapshot_schedules')

    tracker = FieldTracker()

    def __str__(self):
        return 'SnapshotSchedule of %s. Active: %s' % (self.source_volume, self.is_active)

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-snapshot-schedule'


@python_2_unicode_compatible
class Network(core_models.DescribableMixin, structure_models.ServiceProperty):
    is_external = models.BooleanField(default=False)
    type = models.CharField(max_length=50, blank=True)
    segmentation_id = models.IntegerField(null=True)

    class Meta(object):
        unique_together = ('settings', 'backend_id')

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-network'


@python_2_unicode_compatible
class SubNet(core_models.DescribableMixin, structure_models.ServiceProperty):
    network = models.ForeignKey(Network, related_name='subnets')
    cidr = models.CharField(max_length=32, blank=True)
    gateway_ip = models.GenericIPAddressField(protocol='IPv4', null=True)
    allocation_pools = JSONField(default=dict)
    ip_version = models.SmallIntegerField(default=4)
    enable_dhcp = models.BooleanField(default=True)
    dns_nameservers = JSONField(default=list, help_text=_('List of DNS name servers associated with the subnet.'))

    class Meta(object):
        verbose_name = _('Subnet')
        verbose_name_plural = _('Subnets')
        unique_together = ('settings', 'backend_id')

    def __str__(self):
        return '%s (%s)' % (self.name, self.cidr)

    @classmethod
    def get_url_name(cls):
        return 'openstacktenant-subnet'


class InternalIP(openstack_base_models.Port):
    """
    Instance may have several IP addresses in the same subnet
    if shared IPs are implemented using Virtual Router Redundancy Protocol.
    """
    # Name "internal_ips" is reserved by virtual machine mixin and corresponds to list of internal IPs.
    # So another related name should be used.
    instance = models.ForeignKey(Instance, related_name='internal_ips_set', null=True)
    subnet = models.ForeignKey(SubNet, related_name='internal_ips')

    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(max_length=255, null=True)
    settings = models.ForeignKey(structure_models.ServiceSettings, related_name='+')

    class Meta:
        unique_together = ('backend_id', 'settings')
