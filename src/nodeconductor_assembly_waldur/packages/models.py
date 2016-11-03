from __future__ import unicode_literals

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.encoding import python_2_unicode_compatible

from nodeconductor.core import models as core_models
from nodeconductor.structure import models as structure_models
from nodeconductor_openstack.openstack import models as openstack_models, apps as openstack_apps


@python_2_unicode_compatible
class PackageTemplate(core_models.UuidMixin,
                      core_models.NameMixin,
                      core_models.UiDescribableMixin):
    # We do not define permissions for PackageTemplate because we are planning
    # to use them with shared service settings only - thats means that
    # PackageTemplates are visible for all users.
    service_settings = models.ForeignKey(structure_models.ServiceSettings, related_name='+')

    @property
    def price(self):
        return self.components.aggregate(total=models.Sum(
            models.F('price') * models.F('amount'),
            output_field=models.DecimalField(max_digits=13, decimal_places=7)))['total'] or Decimal('0')

    @staticmethod
    def get_required_component_types():
        return (PackageComponent.Types.RAM,
                PackageComponent.Types.CORES,
                PackageComponent.Types.STORAGE)

    def clean(self):
        openstack_type = openstack_apps.OpenStackConfig.service_name
        if self.service_settings.type == openstack_type and not self.service_settings.options.get('is_admin', True):
            raise ValidationError({'service_settings': 'Service settings should support tenant creation.'})
        return self

    def __str__(self):
        return '%s | %s' % (self.name, self.service_settings.type)


@python_2_unicode_compatible
class PackageComponent(models.Model):
    class Meta(object):
        unique_together = ('type', 'template')

    class Types(object):
        RAM = 'ram'
        CORES = 'cores'
        STORAGE = 'storage'

        CHOICES = ((RAM, 'RAM'), (CORES, 'Cores'), (STORAGE, 'Storage'))

    type = models.CharField(max_length=50, choices=Types.CHOICES)
    amount = models.PositiveIntegerField(default=0)
    price = models.DecimalField(default=0, max_digits=13, decimal_places=7,
                                validators=[MinValueValidator(Decimal('0'))], help_text='The price per unit of amount')
    template = models.ForeignKey(PackageTemplate, related_name='components')

    def __str__(self):
        return '%s | %s' % (self.type, self.template.name)


@python_2_unicode_compatible
class OpenStackPackage(core_models.UuidMixin, models.Model):
    """ OpenStackPackage allows to create tenant and service_settings based on PackageTemplate """
    class Permissions(object):
        customer_path = 'tenant__service_project_link__project__customer'
        project_path = 'tenant__service_project_link__project'

    template = models.ForeignKey(PackageTemplate, related_name='openstack_packages',
                                 help_text='Tenant will be created based on this template.')
    tenant = models.ForeignKey(openstack_models.Tenant, related_name='+')
    service_settings = models.ForeignKey(structure_models.ServiceSettings, related_name='+')

    def __str__(self):
        return 'Package "%s" for tenant %s' % (self.template, self.tenant)

    @staticmethod
    def get_quota_to_component_mapping():
        return {
            openstack_models.Tenant.Quotas.ram: PackageComponent.Types.RAM,
            openstack_models.Tenant.Quotas.vcpu: PackageComponent.Types.CORES,
            openstack_models.Tenant.Quotas.storage: PackageComponent.Types.STORAGE,
        }
