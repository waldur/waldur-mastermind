from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import ugettext_lazy as _


class UnitPriceMixin(models.Model):
    """
    Mixin to expose standardized "unit_price" and "unit" field.
    """
    class Meta(object):
        abstract = True

    class Units(object):
        PER_MONTH = 'month'
        PER_HALF_MONTH = 'half_month'
        PER_DAY = 'day'

        CHOICES = ((PER_MONTH, _('Per month')), (PER_HALF_MONTH, _('Per half month')), (PER_DAY, _('Per day')))

    unit_price = models.DecimalField(default=0, max_digits=22, decimal_places=7,
                                     validators=[MinValueValidator(Decimal('0'))])
    unit = models.CharField(default=Units.PER_DAY, max_length=30, choices=Units.CHOICES)
