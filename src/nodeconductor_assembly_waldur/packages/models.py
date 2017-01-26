from __future__ import unicode_literals

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _

from nodeconductor.core import models as core_models
from nodeconductor.structure import models as structure_models
from nodeconductor_openstack.openstack import models as openstack_models, apps as openstack_apps


@python_2_unicode_compatible
class PackageTemplate(core_models.UuidMixin,
                      core_models.NameMixin,
                      core_models.UiDescribableMixin):
    # We do not define permissions for PackageTemplate because we are planning
    # to use them with shared service settings only - it means that
    # PackageTemplates are visible for all users.
    service_settings = models.ForeignKey(structure_models.ServiceSettings, related_name='+')
    archived = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if self.openstack_packages.first():
            raise models.ProtectedError('Current template has linked packages.', [self.openstack_packages.all()])

        super(PackageTemplate, self).save(*args, **kwargs)

    class Categories(object):
        SMALL = 'small'
        MEDIUM = 'medium'
        LARGE = 'large'
        TRIAL = 'trial'

        CHOICES = ((SMALL, 'Small'), (MEDIUM, 'Medium'), (LARGE, 'Large'), (TRIAL, 'Trial'))

    category = models.CharField(max_length=10, choices=Categories.CHOICES, default=Categories.SMALL)

    class Meta(object):
        verbose_name = _('VPC package template')
        verbose_name_plural = _('VPC package templates')

    @property
    def price(self):
        """ Price for whole template for one day """
        return self.components.aggregate(total=models.Sum(
            models.F('price') * models.F('amount'),
            output_field=models.DecimalField(max_digits=22, decimal_places=10)))['total'] or Decimal('0')

    @property
    def monthly_price(self):
        """ Price for one template for 30 days """
        return round(self.price * 30, 2)

    @staticmethod
    def get_required_component_types():
        return (PackageComponent.Types.RAM,
                PackageComponent.Types.CORES,
                PackageComponent.Types.STORAGE)

    @staticmethod
    def get_memory_types():
        return (PackageComponent.Types.RAM,
                PackageComponent.Types.STORAGE)

    def clean(self):
        openstack_type = openstack_apps.OpenStackConfig.service_name

        if not hasattr(self, 'service_settings'):
            raise ValidationError({'service_settings': 'Please select service settings.'})
        if not self.service_settings.shared:
            raise ValidationError({'service_settings': 'PackageTemplate can be created only for shared settings.'})
        if self.service_settings.type == openstack_type and not self.service_settings.options.get('is_admin', True):
            raise ValidationError({'service_settings': 'Service settings should support tenant creation.'})
        if 'external_network_id' not in self.service_settings.options:
            raise ValidationError({'service_settings': 'external_network_id has to be defined for service settings.'})
        return self

    @classmethod
    def get_url_name(cls):
        return 'package-template'

    def __str__(self):
        return '%s | %s' % (self.name, self.service_settings.type)


@python_2_unicode_compatible
class PackageComponent(models.Model):
    PRICE_MAX_DIGITS = 14
    PRICE_DECIMAL_PLACES = 10

    class Meta(object):
        unique_together = ('type', 'template')

    class Types(object):
        RAM = 'ram'
        CORES = 'cores'
        STORAGE = 'storage'

        CHOICES = ((RAM, 'RAM'), (CORES, 'Cores'), (STORAGE, 'Storage'))

    type = models.CharField(max_length=50, choices=Types.CHOICES)
    amount = models.PositiveIntegerField(default=0)
    price = models.DecimalField(default=0, max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES,
                                validators=[MinValueValidator(Decimal('0'))],
                                verbose_name='Price per unit per day')
    template = models.ForeignKey(PackageTemplate, related_name='components')

    def __str__(self):
        return '%s | %s' % (self.type, self.template.name)

    @property
    def monthly_price(self):
        """
        Rounded price for 30-days.

        This price should not be used for calculations.
        Only to display price in human friendly way.
        """
        return round(self.price * 30 * self.amount, 2)


@python_2_unicode_compatible
class OpenStackPackage(core_models.UuidMixin, models.Model):
    """ OpenStackPackage allows to create tenant and service_settings based on PackageTemplate """
    class Permissions(object):
        customer_path = 'tenant__service_project_link__project__customer'
        project_path = 'tenant__service_project_link__project'

    template = models.ForeignKey(PackageTemplate, related_name='openstack_packages',
                                 help_text='Tenant will be created based on this template.',
                                 on_delete=models.PROTECT)
    tenant = models.ForeignKey(openstack_models.Tenant, related_name='+')
    service_settings = models.ForeignKey(structure_models.ServiceSettings, related_name='+', null=True,
                                         on_delete=models.SET_NULL)

    @classmethod
    def get_url_name(cls):
        return 'openstack-package'

    def __str__(self):
        return 'Package "%s" for tenant %s' % (self.template, self.tenant)

    @staticmethod
    def get_quota_to_component_mapping():
        return {
            openstack_models.Tenant.Quotas.ram: PackageComponent.Types.RAM,
            openstack_models.Tenant.Quotas.vcpu: PackageComponent.Types.CORES,
            openstack_models.Tenant.Quotas.storage: PackageComponent.Types.STORAGE,
        }

    class Meta(object):
        verbose_name = _('OpenStack VPC package')
        verbose_name_plural = _('OpenStack VPC packages')
