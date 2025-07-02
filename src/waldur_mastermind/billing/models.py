from typing import cast

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.billing.utils import aggregate_invoice_items_sum
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import utils as invoices_utils

from . import managers


class PriceEstimate(core_models.UuidMixin, models.Model):
    content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ContentType, null=True, related_name="+"
    )
    object_id = models.PositiveIntegerField(null=True)
    scope = GenericForeignKey("content_type", "object_id")

    objects = managers.PriceEstimateManager("scope")
    tracker = cast(FieldInstanceTracker, FieldTracker())

    total = models.FloatField(
        default=0, help_text=_("Predicted price for scope for current month.")
    )

    def __str__(self):
        return f"{self.scope} estimate: {self.total}"

    @classmethod
    def get_estimated_models(cls):
        return structure_models.Project, structure_models.Customer

    def _get_sum(self, year, month, current: bool, tax: bool):
        if not self.scope:
            return 0
        items = invoices_models.InvoiceItem.objects.filter(
            invoice__year=year, invoice__month=month
        ).select_related("invoice")
        if self.content_type.model_class() == structure_models.Project:
            items = items.filter(project__uuid=self.scope.uuid.hex)
        elif self.content_type.model_class() == structure_models.Customer:
            items = items.filter(invoice__customer=self.scope)
        return aggregate_invoice_items_sum(items, current, tax)

    def get_total(self, year, month, current=False):
        return self._get_sum(year, month, current, False)

    def get_tax(self, year, month, current=False):
        return self._get_sum(year, month, current, True)

    def update_total(self):
        current_year = invoices_utils.get_current_year()
        current_month = invoices_utils.get_current_month()
        self.total = self.get_total(current_year, current_month)
