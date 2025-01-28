from django.db import models

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models


class Rule(models.Model):
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


class RulePlans(models.Model):
    rule = models.ForeignKey(Rule, on_delete=models.CASCADE)
    plan = models.ForeignKey(
        marketplace_models.Plan, related_name="+", on_delete=models.CASCADE
    )
    attributes = models.JSONField(blank=True, default=dict)
    limits = models.JSONField(blank=True, default=dict)
