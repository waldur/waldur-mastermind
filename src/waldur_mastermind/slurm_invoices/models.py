from __future__ import unicode_literals

from decimal import Decimal

from django.db import models
from django.core.validators import MinValueValidator
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.common import mixins as common_mixins


class SlurmPackage(common_mixins.ProductCodeMixin,
                   core_models.UuidMixin,
                   core_models.NameMixin,
                   models.Model):

    class Meta(object):
        verbose_name = _('SLURM package')
        verbose_name_plural = _('SLURM packages')

    class Permissions(object):
        customer_path = 'service_settings__customer'

    PRICE_MAX_DIGITS = 14
    PRICE_DECIMAL_PLACES = 10

    service_settings = models.OneToOneField(structure_models.ServiceSettings,
                                            related_name='+',
                                            limit_choices_to={'type': 'SLURM'})

    cpu_price = models.DecimalField(default=0,
                                    verbose_name=_('Price for CPU hour'),
                                    max_digits=PRICE_MAX_DIGITS,
                                    decimal_places=PRICE_DECIMAL_PLACES,
                                    validators=[MinValueValidator(Decimal('0'))])

    gpu_price = models.DecimalField(default=0,
                                    verbose_name=_('Price for GPU hour'),
                                    max_digits=PRICE_MAX_DIGITS,
                                    decimal_places=PRICE_DECIMAL_PLACES,
                                    validators=[MinValueValidator(Decimal('0'))])

    ram_price = models.DecimalField(default=0,
                                    verbose_name=_('Price for GB RAM'),
                                    max_digits=PRICE_MAX_DIGITS,
                                    decimal_places=PRICE_DECIMAL_PLACES,
                                    validators=[MinValueValidator(Decimal('0'))])
