from __future__ import unicode_literals

from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker

from nodeconductor.core import models as core_models
from nodeconductor.logging import models as logging_models
from nodeconductor.structure import models as structure_models
from nodeconductor_assembly_waldur.invoices import models as invoices_models
from nodeconductor_assembly_waldur.invoices import utils as invoices_utils

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

    def update_total(self):
        current_year = invoices_utils.get_current_year()
        current_month = invoices_utils.get_current_month()

        if self.content_type.model_class() == structure_models.Project:
            self.total = sum(item.price
                             for model in invoices_models.InvoiceItem.get_all_models()
                             for item in model.objects.filter(invoice__year=current_year,
                                                              invoice__month=current_month,
                                                              project__uuid=self.scope.uuid.hex))
        elif self.content_type.model_class() == structure_models.Customer:
            try:
                invoice = invoices_models.Invoice.objects.get(
                    customer=self.scope,
                    year=current_year,
                    month=current_month,
                )
                self.total = invoice.total
            except invoices_models.Invoice.DoesNotExist:
                pass
