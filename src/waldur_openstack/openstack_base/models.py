from django.core.validators import MaxValueValidator
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models


@python_2_unicode_compatible
class BaseSecurityGroupRule(models.Model):
    TCP = 'tcp'
    UDP = 'udp'
    ICMP = 'icmp'

    CHOICES = (
        (TCP, 'tcp'),
        (UDP, 'udp'),
        (ICMP, 'icmp'),
    )

    # Empty string represents any protocol
    protocol = models.CharField(max_length=4, blank=True, choices=CHOICES)
    from_port = models.IntegerField(validators=[MaxValueValidator(65535)], null=True)
    to_port = models.IntegerField(validators=[MaxValueValidator(65535)], null=True)
    cidr = models.CharField(max_length=32, blank=True)

    backend_id = models.CharField(max_length=128, blank=True)

    class Meta(object):
        abstract = True

    def __str__(self):
        return '%s (%s): %s (%s -> %s)' % \
               (self.security_group, self.protocol, self.cidr, self.from_port, self.to_port)


@python_2_unicode_compatible
class Port(core_models.BackendModelMixin, models.Model):
    # TODO: Use dedicated field: https://github.com/django-macaddress/django-macaddress
    mac_address = models.CharField(max_length=32, blank=True)
    ip4_address = models.GenericIPAddressField(null=True, blank=True, protocol='IPv4')
    ip6_address = models.GenericIPAddressField(null=True, blank=True, protocol='IPv6')
    backend_id = models.CharField(max_length=255, blank=True)

    class Meta(object):
        abstract = True

    def __str__(self):
        return self.ip4_address or self.ip6_address or 'Not initialized'

    @classmethod
    def get_backend_fields(cls):
        return super(Port, cls).get_backend_fields() + ('ip4_address', 'ip6_address', 'mac_address')


class BaseImage(structure_models.ServiceProperty):
    min_disk = models.PositiveIntegerField(default=0, help_text=_('Minimum disk size in MiB'))
    min_ram = models.PositiveIntegerField(default=0, help_text=_('Minimum memory size in MiB'))

    class Meta(structure_models.ServiceProperty.Meta):
        abstract = True

    @classmethod
    def get_backend_fields(cls):
        return super(BaseImage, cls).get_backend_fields() + ('min_disk', 'min_ram')
