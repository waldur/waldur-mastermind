from django.core.validators import MaxValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from waldur_core.core import models as core_models
from waldur_core.core.fields import JSONField
from waldur_core.structure import models as structure_models


class BaseSecurityGroupRule(core_models.DescribableMixin, models.Model):
    TCP = 'tcp'
    UDP = 'udp'
    ICMP = 'icmp'

    PROTOCOLS = (
        (TCP, 'tcp'),
        (UDP, 'udp'),
        (ICMP, 'icmp'),
    )

    INGRESS = 'ingress'
    EGRESS = 'egress'

    DIRECTIONS = (
        (INGRESS, 'ingress'),
        (EGRESS, 'egress'),
    )

    IPv4 = 'IPv4'
    IPv6 = 'IPv6'

    ETHER_TYPES = (
        (IPv4, 'IPv4'),
        (IPv6, 'IPv6'),
    )

    # Empty string represents any protocol
    protocol = models.CharField(max_length=40, blank=True, choices=PROTOCOLS)
    from_port = models.IntegerField(validators=[MaxValueValidator(65535)], null=True)
    to_port = models.IntegerField(validators=[MaxValueValidator(65535)], null=True)
    cidr = models.CharField(max_length=255, blank=True, null=True)
    direction = models.CharField(max_length=8, default=INGRESS, choices=DIRECTIONS)
    ethertype = models.CharField(max_length=40, default=IPv4, choices=ETHER_TYPES)

    backend_id = models.CharField(max_length=36, blank=True)

    class Meta:
        abstract = True

    def __str__(self):
        return '{} ({}): {} ({} -> {})'.format(
            self.security_group,
            self.protocol,
            self.cidr,
            self.from_port,
            self.to_port,
        )


class Port(core_models.BackendModelMixin, models.Model):
    # TODO: Use dedicated field: https://github.com/django-macaddress/django-macaddress
    mac_address = models.CharField(max_length=32, blank=True)
    fixed_ips = JSONField(
        default=list,
        help_text=_(
            'A list of tuples (ip_address, subnet_id), where ip_address can be both IPv4 and IPv6 '
            'and subnet_id is a backend id of the subnet'
        ),
    )
    backend_id = models.CharField(max_length=255, blank=True)
    allowed_address_pairs = JSONField(
        default=list,
        help_text=_(
            'A server can send a packet with source address which matches one of the specified allowed address pairs.'
        ),
    )
    device_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )
    device_owner = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    class Meta:
        abstract = True

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            'fixed_ips',
            'mac_address',
            'allowed_address_pairs',
            'device_id',
            'device_owner',
        )


class BaseImage(structure_models.ServiceProperty):
    min_disk = models.PositiveIntegerField(
        default=0, help_text=_('Minimum disk size in MiB')
    )
    min_ram = models.PositiveIntegerField(
        default=0, help_text=_('Minimum memory size in MiB')
    )

    class Meta(structure_models.ServiceProperty.Meta):
        abstract = True

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + ('min_disk', 'min_ram')


class BaseVolumeType(core_models.DescribableMixin, structure_models.ServiceProperty):
    class Meta:
        unique_together = ('settings', 'backend_id')
        abstract = True

    def __str__(self):
        return self.name


class BaseSubNet(models.Model):
    class Meta:
        abstract = True

    cidr = models.CharField(max_length=32, blank=True)
    gateway_ip = models.GenericIPAddressField(protocol='IPv4', null=True)
    allocation_pools = JSONField(default=dict)
    ip_version = models.SmallIntegerField(default=4)
    enable_dhcp = models.BooleanField(default=True)
    dns_nameservers = JSONField(
        default=list,
        help_text=_('List of DNS name servers associated with the subnet.'),
    )
    is_connected = models.BooleanField(
        default=True, help_text=_('Is subnet connected to the default tenant router.')
    )


class BaseServerGroup(models.Model):
    class Meta:
        abstract = True

    AFFINITY = 'affinity'

    POLICIES = ((AFFINITY, 'Affinity'),)

    policy = models.CharField(max_length=40, blank=True, choices=POLICIES)
