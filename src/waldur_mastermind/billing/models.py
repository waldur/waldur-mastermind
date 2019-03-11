from __future__ import unicode_literals

from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker

from waldur_core.core import models as core_models
from waldur_core.logging import models as logging_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import utils as invoices_utils

from . import exceptions, managers


class PriceEstimate(logging_models.AlertThresholdMixin, core_models.UuidMixin, models.Model):
    content_type = models.ForeignKey(ContentType, null=True, related_name='+')
    object_id = models.PositiveIntegerField(null=True)
    scope = GenericForeignKey('content_type', 'object_id')

    objects = managers.PriceEstimateManager('scope')
    tracker = FieldTracker()

    total = models.FloatField(default=0, help_text=_('Predicted price for scope for current month.'))
    limit = models.FloatField(default=-1, help_text=_('Price limit of a scope object in current month. '
                                                      '-1 means no limit.'))

    def is_over_threshold(self):  # For AlertThresholdMixin
        return self.total > self.threshold

    def validate_limit(self):
        if self.limit != -1 and self.total > self.limit:
            raise exceptions.PriceEstimateLimitExceeded(self)

    @classmethod
    def get_estimated_models(cls):
        return structure_models.Project, structure_models.Customer

    def _get_sum(self, year, month, field):
        items = invoices_models.GenericInvoiceItem.objects.filter(
            invoice__year=year,
            invoice__month=month
        )
        if self.content_type.model_class() == structure_models.Project:
            items = items.filter(project__uuid=self.scope.uuid.hex)
        elif self.content_type.model_class() == structure_models.Customer:
            items = items.filter(invoice__customer=self.scope)
        return sum(getattr(item, field) for item in items)

    def get_total(self, year, month, current=False):
        return self._get_sum(year, month, current and 'price_current' or 'price')

    def get_tax(self, year, month, current=False):
        return self._get_sum(year, month, current and 'tax_current' or 'tax')

    def update_total(self):
        current_year = invoices_utils.get_current_year()
        current_month = invoices_utils.get_current_month()
        self.total = self.get_total(current_year, current_month)
