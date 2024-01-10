import logging

from django.utils import timezone

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.policy import models as policy_models

logger = logging.getLogger(__name__)


def project_estimated_cost_policy_handler(sender, instance, created=False, **kwargs):
    if not isinstance(instance.scope, structure_models.Project):
        return

    project = instance.scope
    policies = policy_models.ProjectEstimatedCostPolicy.objects.filter(project=project)

    for policy in policies:
        if not policy.has_fired and policy.is_triggered():
            policy.has_fired = True
            policy.fired_datetime = timezone.now()
            policy.save()

            for action in policy.get_one_time_actions():
                action(policy)
                logger.info(
                    "%s action has been triggered for project %s. Policy UUID: %s",
                    action.__name__,
                    policy.project.name,
                    policy.uuid.hex,
                )

        elif policy.has_fired and not policy.is_triggered():
            policy.has_fired = False
            policy.save()


def project_estimated_cost_policy_handler_for_observable_class(
    sender, instance, created=False, **kwargs
):
    if not isinstance(instance, marketplace_models.Resource):
        return

    resource = instance
    policies = policy_models.ProjectEstimatedCostPolicy.objects.filter(
        project=resource.project
    )

    for policy in policies:
        if policy.is_triggered():
            for action in policy.get_not_one_time_actions():
                action(policy, created)
                logger.info(
                    "%s action has been triggered for project %s. Policy UUID: %s",
                    action.__name__,
                    policy.project.name,
                    policy.uuid.hex,
                )
