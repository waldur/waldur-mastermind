from __future__ import unicode_literals

from django.db import models
from django.utils.translation import ugettext_lazy as _
from django.utils.encoding import python_2_unicode_compatible

from waldur_core.core.fields import JSONField
from waldur_core.logging.loggers import LoggableMixin
from waldur_core.structure import models as structure_models


class RijkscloudService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project,
        related_name='rijkscloud_services',
        through='RijkscloudServiceProjectLink'
    )

    class Meta:
        unique_together = ('customer', 'settings')
        verbose_name = _('Rijkscloud provider')
        verbose_name_plural = _('Rijkscloud providers')

    @classmethod
    def get_url_name(cls):
        return 'rijkscloud'


class RijkscloudServiceProjectLink(structure_models.ServiceProjectLink):

    service = models.ForeignKey(RijkscloudService)

    class Meta(structure_models.ServiceProjectLink.Meta):
        verbose_name = _('Rijkscloud provider project link')
        verbose_name_plural = _('Rijkscloud provider project links')

    @classmethod
    def get_url_name(cls):
        return 'rijkscloud-spl'


class Flavor(LoggableMixin, structure_models.ServiceProperty):
    cores = models.PositiveSmallIntegerField(help_text=_('Number of cores in a VM'))
    ram = models.PositiveIntegerField(help_text=_('Memory size in MiB'))

    @classmethod
    def get_url_name(cls):
        return 'rijkscloud-flavor'

    @classmethod
    def get_backend_fields(cls):
        readonly_fields = super(Flavor, cls).get_backend_fields()
        return readonly_fields + ('cores', 'ram')


class Volume(structure_models.Volume):
    service_project_link = models.ForeignKey(
        RijkscloudServiceProjectLink,
        related_name='volumes',
        on_delete=models.PROTECT
    )
    metadata = JSONField(blank=True)

    @classmethod
    def get_url_name(cls):
        return 'rijkscloud-volume'

    @classmethod
    def get_backend_fields(cls):
        return super(Volume, cls).get_backend_fields() + (
            'name', 'size', 'metadata', 'runtime_state')


@python_2_unicode_compatible
class Instance(structure_models.VirtualMachine):
    service_project_link = models.ForeignKey(
        RijkscloudServiceProjectLink,
        related_name='instances',
        on_delete=models.PROTECT
    )
    flavor_name = models.CharField(max_length=255, blank=True)
    floating_ip = models.ForeignKey('FloatingIP', blank=True, null=True)
    internal_ip = models.ForeignKey('InternalIP')

    @classmethod
    def get_url_name(cls):
        return 'rijkscloud-instance'

    @classmethod
    def get_backend_fields(cls):
        return super(Instance, cls).get_backend_fields() + (
            'flavor_name', 'ram', 'cores', 'runtime_state'
        )

    @property
    def external_ips(self):
        if self.floating_ip:
            return [self.floating_ip.address]

    @property
    def internal_ips(self):
        return [self.internal_ip.address]

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class FloatingIP(structure_models.ServiceProperty):
    address = models.GenericIPAddressField(protocol='IPv4', null=True)
    is_available = models.BooleanField(default=True)

    class Meta:
        verbose_name = _('Floating IP')
        verbose_name_plural = _('Floating IPs')
        unique_together = ('settings', 'backend_id')

    @classmethod
    def get_url_name(cls):
        return 'rijkscloud-fip'

    @classmethod
    def get_backend_fields(cls):
        return super(FloatingIP, cls).get_backend_fields() + ('address', 'is_available')

    def __str__(self):
        return self.address


@python_2_unicode_compatible
class Network(structure_models.ServiceProperty):
    @classmethod
    def get_url_name(cls):
        return 'rijkscloud-network'

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class SubNet(structure_models.ServiceProperty):
    network = models.ForeignKey(Network, related_name='subnets')
    cidr = models.CharField(max_length=32)
    gateway_ip = models.GenericIPAddressField(protocol='IPv4')
    allocation_pools = JSONField()
    dns_nameservers = JSONField(help_text=_('List of DNS name servers associated with the subnet.'))

    class Meta(object):
        verbose_name = _('Subnet')
        verbose_name_plural = _('Subnets')
        unique_together = ('settings', 'backend_id')

    def __str__(self):
        return '%s (%s)' % (self.name, self.cidr)

    @classmethod
    def get_url_name(cls):
        return 'rijkscloud-subnet'


@python_2_unicode_compatible
class InternalIP(structure_models.ServiceProperty):
    address = models.GenericIPAddressField(protocol='IPv4')
    is_available = models.BooleanField(default=True)
    subnet = models.ForeignKey(SubNet, related_name='internal_ips')

    @classmethod
    def get_backend_fields(cls):
        return super(InternalIP, cls).get_backend_fields() + ('address', 'is_available')

    @classmethod
    def get_url_name(cls):
        return 'rijkscloud-internal-ip'

    class Meta:
        verbose_name = _('Internal IP')
        verbose_name_plural = _('Internal IPs')
        unique_together = ('settings', 'backend_id')

    def __str__(self):
        return '%s (%s)' % (self.address, self.subnet.name)
