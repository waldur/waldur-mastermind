from django.db import models
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.permissions.models import Role
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models


class Rule(TimeStampedModel, core_models.UuidMixin):
    class Permissions:
        customer_path = "customer"

    user_affiliations = models.JSONField(
        default=list,
        blank=True,
    )
    user_email_patterns = models.JSONField(
        default=list,
        blank=True,
    )
    customer = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE)
    plans = models.ManyToManyField(marketplace_models.Plan, through="RulePlans")
    project_role = models.ForeignKey(
        to=Role,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    @classmethod
    def get_url_name(cls):
        return "autoprovisioning-rule"


class RulePlans(TimeStampedModel, core_models.UuidMixin):
    class Permissions:
        customer_path = "rule__customer"

    rule = models.ForeignKey(Rule, on_delete=models.CASCADE)
    plan = models.ForeignKey(
        marketplace_models.Plan, related_name="+", on_delete=models.CASCADE
    )
    attributes = models.JSONField(blank=True, default=dict)
    limits = models.JSONField(blank=True, default=dict)

    @classmethod
    def get_url_name(cls):
        return "autoprovisioning-rule-plan"
