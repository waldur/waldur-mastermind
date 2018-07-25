from __future__ import unicode_literals

from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from libcloud.compute.drivers.ec2 import REGION_DETAILS

from waldur_core.core.fields import JSONField
from waldur_core.core.models import RuntimeStateMixin
from waldur_core.quotas.fields import CounterQuotaField
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure import models as structure_models
from waldur_core.structure.utils import get_coordinates_by_ip


class AWSService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project, related_name='aws_services', through='AWSServiceProjectLink')

    class Meta(structure_models.Service.Meta):
        verbose_name = _('AWS provider')
        verbose_name_plural = _('AWS providers')

    class Quotas(QuotaModelMixin.Quotas):
        instance_count = CounterQuotaField(
            target_models=lambda: [Instance],
            path_to_scope='service_project_link.service'
        )

        volume_count = CounterQuotaField(
            target_models=lambda: [Volume],
            path_to_scope='service_project_link.service'
        )

    @classmethod
    def get_url_name(cls):
        return 'aws'


class AWSServiceProjectLink(structure_models.CloudServiceProjectLink):
    service = models.ForeignKey(AWSService)

    class Meta(structure_models.CloudServiceProjectLink.Meta):
        verbose_name = _('AWS provider project link')
        verbose_name_plural = _('AWS provider project links')

    @classmethod
    def get_url_name(cls):
        return 'aws-spl'


class Region(structure_models.GeneralServiceProperty):
    class Meta:
        ordering = ['name']

    @classmethod
    def get_url_name(cls):
        return 'aws-region'


@python_2_unicode_compatible
class Image(structure_models.GeneralServiceProperty):
    class Meta:
        ordering = ['name']

    region = models.ForeignKey(Region)

    def __str__(self):
        return '{0} | {1}'.format(self.name, self.region.name)

    @classmethod
    def get_url_name(cls):
        return 'aws-image'

    @classmethod
    def get_backend_fields(cls):
        return super(Image, cls).get_backend_fields() + ('region',)


class Size(structure_models.GeneralServiceProperty):
    class Meta:
        ordering = ['cores', 'ram']

    regions = models.ManyToManyField(Region)
    cores = models.PositiveSmallIntegerField(help_text=_('Number of cores in a VM'))
    ram = models.PositiveIntegerField(help_text=_('Memory size in MiB'))
    disk = models.PositiveIntegerField(help_text=_('Disk size in MiB'))
    price = models.DecimalField(_('Hourly price rate'), default=0, max_digits=11, decimal_places=5)

    @classmethod
    def get_url_name(cls):
        return 'aws-size'

    @classmethod
    def get_backend_fields(cls):
        return super(Size, cls).get_backend_fields() + ('cores', 'ram', 'disk', 'price', 'regions')


class Instance(structure_models.VirtualMachine):
    service_project_link = models.ForeignKey(
        AWSServiceProjectLink, related_name='instances', on_delete=models.PROTECT)

    region = models.ForeignKey(Region)
    public_ips = JSONField(default=list, help_text=_('List of public IP addresses'), blank=True)
    private_ips = JSONField(default=list, help_text=_('List of private IP addresses'), blank=True)
    size_backend_id = models.CharField(max_length=150, blank=True)

    def increase_backend_quotas_usage(self, validate=True):
        spl = self.service_project_link
        spl.add_quota_usage(spl.Quotas.storage, self.disk, validate=validate)
        spl.add_quota_usage(spl.Quotas.ram, self.ram, validate=validate)
        spl.add_quota_usage(spl.Quotas.vcpu, self.cores, validate=validate)

    def decrease_backend_quotas_usage(self):
        self.service_project_link.add_quota_usage(self.service_project_link.Quotas.storage, -self.disk)
        self.service_project_link.add_quota_usage(self.service_project_link.Quotas.ram, -self.ram)
        self.service_project_link.add_quota_usage(self.service_project_link.Quotas.vcpu, -self.cores)

    @property
    def external_ips(self):
        return self.public_ips

    @property
    def internal_ips(self):
        return self.private_ips

    def detect_coordinates(self):
        if self.external_ips:
            return get_coordinates_by_ip(self.external_ips[0])
        region = self.region.backend_id
        endpoint = REGION_DETAILS[region]['endpoint']
        return get_coordinates_by_ip(endpoint)

    @classmethod
    def get_url_name(cls):
        return 'aws-instance'

    @classmethod
    def get_backend_fields(cls):
        return super(Instance, cls).get_backend_fields() + ('runtime_state',)

    @classmethod
    def get_online_state(cls):
        return 'running'

    @classmethod
    def get_offline_state(cls):
        return 'stopped'


class Volume(RuntimeStateMixin, structure_models.NewResource):
    service_project_link = models.ForeignKey(
        AWSServiceProjectLink, related_name='volumes', on_delete=models.PROTECT)

    VOLUME_TYPES = (
        ('gp2', _('General Purpose SSD')),
        ('io1', _('Provisioned IOPS SSD')),
        ('standard', _('Magnetic volumes'))
    )
    size = models.PositiveIntegerField(help_text=_('Size of volume in gigabytes'))
    region = models.ForeignKey(Region)
    volume_type = models.CharField(max_length=8, choices=VOLUME_TYPES)
    device = models.CharField(max_length=128, blank=True, null=True)
    instance = models.ForeignKey(Instance, blank=True, null=True)

    @classmethod
    def get_url_name(cls):
        return 'aws-volume'

    @classmethod
    def get_backend_fields(cls):
        return super(Volume, cls).get_backend_fields() + ('name', 'device', 'size', 'volume_type', 'runtime_state')
