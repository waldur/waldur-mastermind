from django.db import models
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMIntegerField, transition
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import enums


class DryRun(
    core_models.UuidMixin,
    core_models.ErrorMessageMixin,
    TimeStampedModel,
):
    order = models.ForeignKey(
        on_delete=models.SET_NULL,
        to=marketplace_models.Order,
        null=True,
    )
    order_attributes = models.JSONField(blank=True, default=dict)
    order_plan = models.ForeignKey(
        on_delete=models.CASCADE, to=marketplace_models.Plan, blank=True, null=True
    )
    order_offering = models.ForeignKey(
        on_delete=models.SET_NULL, null=True, to=marketplace_models.Offering
    )
    order_type = models.CharField(max_length=255)
    state = FSMIntegerField(
        default=enums.DryRunStates.PENDING, choices=enums.DryRunStates.choices
    )
    output = models.TextField(blank=True)

    class Meta:
        verbose_name = _("Dry run")
        ordering = ("created",)

    class Permissions:
        customer_path = "order_offering__customer"

    @transition(
        field=state,
        source=enums.DryRunStates.PENDING,
        target=enums.DryRunStates.EXECUTING,
    )
    def set_state_executing(self):
        pass

    @transition(
        field=state, source=enums.DryRunStates.EXECUTING, target=enums.DryRunStates.DONE
    )
    def set_ok(self):
        pass

    @transition(field=state, source="*", target=enums.DryRunStates.ERRED)
    def set_erred(self):
        pass
