from __future__ import unicode_literals

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils.encoding import python_2_unicode_compatible

from nodeconductor.core import models as core_models


@python_2_unicode_compatible
class PackageTemplate(core_models.UuidMixin,
                      core_models.NameMixin,
                      core_models.UiDescribableMixin):
    class Type(object):
        OPENSTACK = 'openstack'

        CHOICES = ((OPENSTACK, 'OpenStack'),)

    type = models.CharField(max_length=10, choices=Type.CHOICES, default=Type.OPENSTACK)

    @property
    def price(self):
        return self.components.aggregate(total=models.Sum(
            models.F('price') * models.F('amount'),
            output_field=models.DecimalField(decimal_places=2)))['total'] or Decimal('0.00')

    @staticmethod
    def get_required_component_types():
        return (PackageComponent.Type.RAM,
                PackageComponent.Type.CORES,
                PackageComponent.Type.STORAGE)

    def __str__(self):
        return '%s | %s' % (self.name, self.type)


@python_2_unicode_compatible
class PackageComponent(models.Model):
    class Meta(object):
        unique_together = ('type', 'template')

    class Type(object):
        RAM = 'ram'
        CORES = 'cores'
        STORAGE = 'storage'

        CHOICES = ((RAM, 'RAM'), (CORES, 'Cores'), (STORAGE, 'Storage'))

    type = models.CharField(max_length=15, choices=Type.CHOICES)
    amount = models.PositiveIntegerField(default=0)
    price = models.DecimalField(default=0, max_digits=6, decimal_places=2,
                                validators=[MinValueValidator(Decimal('0'))], help_text='The price per unit of amount')
    template = models.ForeignKey(PackageTemplate, related_name='components')

    def __str__(self):
        return '%s | %s' % (self.type, self.template.name)
