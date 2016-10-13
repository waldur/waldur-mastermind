from __future__ import unicode_literals

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils.encoding import python_2_unicode_compatible

from nodeconductor.core import models as core_models
from nodeconductor.structure import models as structure_models
from nodeconductor_openstack import models as openstack_models


@python_2_unicode_compatible
class PackageTemplate(core_models.UuidMixin,
                      core_models.NameMixin,
                      core_models.UiDescribableMixin):
    service_settings = models.ForeignKey(structure_models.ServiceSettings)

    @property
    def price(self):
        return self.components.aggregate(total=models.Sum(
            models.F('price') * models.F('amount'),
            output_field=models.DecimalField(max_digits=13, decimal_places=7)))['total'] or Decimal('0')

    @staticmethod
    def get_required_component_types():
        return (PackageComponent.Type.RAM,
                PackageComponent.Type.CORES,
                PackageComponent.Type.STORAGE)

    def __str__(self):
        return '%s | %s' % (self.name, self.service_settings.type)


@python_2_unicode_compatible
class PackageComponent(models.Model):
    class Meta(object):
        unique_together = ('type', 'template')

    class Type(object):
        RAM = 'ram'
        CORES = 'cores'
        STORAGE = 'storage'

        CHOICES = ((RAM, 'RAM'), (CORES, 'Cores'), (STORAGE, 'Storage'))

    type = models.CharField(max_length=50, choices=Type.CHOICES)
    amount = models.PositiveIntegerField(default=0)
    price = models.DecimalField(default=0, max_digits=13, decimal_places=7,
                                validators=[MinValueValidator(Decimal('0'))], help_text='The price per unit of amount')
    template = models.ForeignKey(PackageTemplate, related_name='components')

    def __str__(self):
        return '%s | %s' % (self.type, self.template.name)


@python_2_unicode_compatible
class OpenStackPackage(core_models.UuidMixin, core_models.NameMixin, core_models.DescribableMixin, models.Model):
    """ OpenStackPackage allows to create OpenStack tenant based on PackageTemplate """
    project = models.ForeignKey(structure_models.Project, related_name='packages',
                                help_text='Tenant will be created in this project.')
    template = models.ForeignKey(PackageTemplate, help_text='Tenant will be created based on this template.')
    tenant = models.ForeignKey(openstack_models.Tenant)
    service = models.ForeignKey(openstack_models.OpenStackService, null=True)

    def __str__(self):
        return 'Package "%s" for customer %s' % (self.template, self.customer)
