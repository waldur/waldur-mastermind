import logging

from django.conf import settings
from django.db import models
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.billing import models as billing_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import policy_actions

logger = logging.getLogger(__name__)


class Policy(
    TimeStampedModel,
    core_models.UuidMixin,
):
    trigger_class = billing_models.PriceEstimate
    observable_classes = [marketplace_models.Resource]
    available_actions = NotImplemented

    has_fired = models.BooleanField(default=False)
    fired_datetime = models.DateTimeField(null=True, blank=True, editable=False)
    created_by = models.ForeignKey(
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name='+',
        blank=True,
        null=True,
    )
    actions = NotImplemented

    def is_triggered(self):
        """Checking if the policy needs to be applied."""
        raise NotImplementedError()

    def get_all_actions(self):
        actions = []

        for action_name in self.actions.split(','):
            if action_name in [a.__name__ for a in self.available_actions]:
                actions.append(
                    [a for a in self.available_actions if a.__name__ == action_name][0]
                )

        return actions

    def get_not_one_time_actions(self):
        actions = self.get_all_actions()
        return [a for a in actions if not getattr(a, 'one_time_action', False)]

    def get_one_time_actions(self):
        actions = self.get_all_actions()
        return [a for a in actions if getattr(a, 'one_time_action', False)]

    class Meta:
        abstract = True


class ProjectPolicy(Policy):
    class Permissions:
        customer_path = 'project__customer'

    available_actions = {
        policy_actions.notify_project_team,
        policy_actions.notify_organization_owners,
        policy_actions.block_creation_of_new_resources,
        policy_actions.block_modification_of_existing_resources,
        policy_actions.terminate_resources,
        policy_actions.request_downscaling,
    }

    project = models.ForeignKey(structure_models.Project, on_delete=models.CASCADE)
    actions = models.CharField(max_length=255)

    class Meta:
        abstract = True


class ProjectEstimatedCostPolicy(ProjectPolicy):
    limit_cost = models.IntegerField()

    def is_triggered(self):
        try:
            price_estimate = billing_models.PriceEstimate.objects.get(
                scope=self.project
            )
            return price_estimate.total > self.limit_cost
        except billing_models.PriceEstimate.DoesNotExist:
            return False

    class Meta:
        verbose_name_plural = "Project estimated cost policies"
