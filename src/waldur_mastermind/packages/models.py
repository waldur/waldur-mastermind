from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_openstack.openstack import models as openstack_models, apps as openstack_apps
from waldur_mastermind.common import mixins as common_mixins

from waldur_mastermind.common.utils import quantize_price


class PackageTemplate(core_models.UuidMixin,
                      core_models.NameMixin,
                      common_mixins.ProductCodeMixin,
                      core_models.UiDescribableMixin,):
    # We do not define permissions for PackageTemplate because we are planning
    # to use them with shared service settings only - it means that
    # PackageTemplates are visible for all users.
    service_settings = models.ForeignKey(on_delete=models.CASCADE, to=structure_models.ServiceSettings, related_name='+')
    archived = models.BooleanField(default=False, help_text=_('Forbids creation of new packages.'))
    tracker = FieldTracker()
    unit = models.CharField(default=common_mixins.UnitPriceMixin.Units.PER_DAY, max_length=30,
                            choices=common_mixins.UnitPriceMixin.Units.CHOICES)

    class Categories:
        SMALL = 'small'
        MEDIUM = 'medium'
        LARGE = 'large'
        TRIAL = 'trial'

        CHOICES = ((SMALL, _('Small')), (MEDIUM, _('Medium')), (LARGE, _('Large')), (TRIAL, _('Trial')))

    category = models.CharField(max_length=10, choices=Categories.CHOICES, default=Categories.SMALL)

    class Meta:
        verbose_name = _('VPC package template')
        verbose_name_plural = _('VPC package templates')

    @property
    def price(self):
        """
        Price for whole template for one day.
        :rtype: Decimal
        """
        if self.unit == common_mixins.UnitPriceMixin.Units.PER_DAY:
            price = self.components.aggregate(total=models.Sum(
                models.F('price') * models.F('amount'),
                output_field=models.DecimalField(max_digits=22, decimal_places=10)))['total'] or Decimal('0')
            return quantize_price(price)

    @property
    def monthly_price(self):
        """
        Return price for one month in case of monthly billing plan or price for 30 days otherwise.
        :rtype: Decimal
        """
        if self.unit == common_mixins.UnitPriceMixin.Units.PER_MONTH:
            price = self.components.aggregate(total=models.Sum(
                models.F('price') * models.F('amount'),
                output_field=models.DecimalField(max_digits=22, decimal_places=10)))['total'] or Decimal('0')
            return quantize_price(price)
        elif self.unit == common_mixins.UnitPriceMixin.Units.PER_DAY:
            return self.price * 30

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
            raise ValidationError({'service_settings': _('Please select service settings.')})
        if not self.service_settings.shared:
            raise ValidationError({'service_settings': _('PackageTemplate can be created only for shared settings.')})
        if self.service_settings.type == openstack_type and not self.service_settings.options.get('is_admin', True):
            raise ValidationError({'service_settings': _('Service settings should support tenant creation.')})
        if 'external_network_id' not in self.service_settings.options:
            raise ValidationError(
                {'service_settings': _('external_network_id has to be defined for service settings.')})
        return self

    def is_read_only(self):
        return self.openstack_packages.exists()

    def __str__(self):
        return '%s | %s' % (self.name, self.service_settings.type)


class PackageComponent(models.Model):
    PRICE_MAX_DIGITS = 14
    PRICE_DECIMAL_PLACES = 10

    class Meta:
        unique_together = ('type', 'template')

    class Types:
        RAM = 'ram'
        CORES = 'cores'
        STORAGE = 'storage'

        CHOICES = ((RAM, 'RAM'), (CORES, 'Cores'), (STORAGE, 'Storage'))

    type = models.CharField(max_length=50, choices=Types.CHOICES)
    amount = models.PositiveIntegerField(default=0)
    price = models.DecimalField(default=0, max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES,
                                validators=[MinValueValidator(Decimal('0'))],
                                verbose_name=_('Price per unit'))
    template = models.ForeignKey(on_delete=models.CASCADE, to=PackageTemplate, related_name='components')

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


class OpenStackPackage(core_models.UuidMixin, models.Model):
    """ OpenStackPackage allows to create tenant and service_settings based on PackageTemplate """
    class Permissions:
        customer_path = 'tenant__service_project_link__project__customer'
        project_path = 'tenant__service_project_link__project'

    template = models.ForeignKey(PackageTemplate, related_name='openstack_packages',
                                 help_text=_('Tenant will be created based on this template.'),
                                 on_delete=models.PROTECT)
    tenant = models.OneToOneField(openstack_models.Tenant, related_name='+', on_delete=models.CASCADE)
    service_settings = models.ForeignKey(structure_models.ServiceSettings, related_name='+', null=True,
                                         on_delete=models.SET_NULL)

    def __str__(self):
        return 'Package "%s" for tenant %s' % (self.template, self.tenant)

    @staticmethod
    def get_quota_to_component_mapping():
        return {
            openstack_models.Tenant.Quotas.ram: PackageComponent.Types.RAM,
            openstack_models.Tenant.Quotas.vcpu: PackageComponent.Types.CORES,
            openstack_models.Tenant.Quotas.storage: PackageComponent.Types.STORAGE,
        }

    def get_quota_usage(self):
        """
        Returns map:
        {
            'ram': ram_quota.usage,
            'cores': vcpu_quota.usage,
            'storage': storage_quota.usage,
        }
        """
        mapping = self.get_quota_to_component_mapping()
        usage = {}
        quotas = {quota.name: quota.usage for quota in self.tenant.quotas.all()}
        for quota, component_type in mapping.items():
            if quota.name in quotas:
                usage[component_type] = quotas[quota.name]
        return usage

    class Meta:
        verbose_name = _('OpenStack VPC package')
        verbose_name_plural = _('OpenStack VPC packages')
