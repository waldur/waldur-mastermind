from __future__ import unicode_literals

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils.encoding import python_2_unicode_compatible

from nodeconductor.core import models as core_models


@python_2_unicode_compatible
class PackageTemplate(core_models.UuidMixin,
                      core_models.NameMixin,
                      core_models.DescribableMixin):
    class Type(object):
        OPENSTACK = 'openstack'

        CHOICES = ((OPENSTACK, 'OpenStack'),)

    type = models.CharField(max_length=10, choices=Type.CHOICES, default=Type.OPENSTACK)

    @property
    def price(self):
        return self.components.aggregate(total=models.Sum('price', field='price*amount'))['total']

    def __str__(self):
        return '%s | %s' % (self.name, self.type)


@python_2_unicode_compatible
class PackageComponent(models.Model):
    class Component(object):
        RAM = 'ram'
        CORES = 'cores'
        STORAGE = 'storage'

        CHOICES = ((RAM, 'RAM'), (CORES, 'Cores'), (STORAGE, 'Storage'))

    name = models.CharField(max_length=15, choices=Component.CHOICES)
    amount = models.PositiveIntegerField(default=0)
    price = models.DecimalField(default=0, max_digits=10, decimal_places=4,
                                validators=[MinValueValidator(Decimal('0'))], help_text='Price per 1 amount')
    template = models.ForeignKey(PackageTemplate, related_name='components')

    def __str__(self):
        return '%s | %s' % (self.name, self.template.name)
