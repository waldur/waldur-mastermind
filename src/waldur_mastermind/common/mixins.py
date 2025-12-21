from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from waldur_mastermind.common.enums import Units

PRICE_MAX_DIGITS = 22

PRICE_DECIMAL_PLACES = 10


class UnitPriceMixin(models.Model):
    """
    Mixin to expose standardized "unit_price" and "unit" field.
    """

    class Meta:
        abstract = True

    unit_price = models.DecimalField(
        default=0,
        max_digits=PRICE_MAX_DIGITS,
        decimal_places=PRICE_DECIMAL_PLACES,
        validators=[MinValueValidator(Decimal("0"))],
    )
    unit = models.CharField(default=Units.PER_DAY, max_length=30, choices=Units.choices)


class ProductCodeMixin(models.Model):
    class Meta:
        abstract = True

    # article code is used for encoding product category in accounting software
    article_code = models.CharField(max_length=30, blank=True)


class BackendMetadataMixin(models.Model):
    class Meta:
        abstract = True

    backend_metadata = models.JSONField(
        default=dict,
        blank=True,
    )
