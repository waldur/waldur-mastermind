import datetime
import logging

from django.core import exceptions as django_exceptions
from django.db.models import Q

from waldur_core.core import models as core_models
from waldur_core.core.utils import month_start
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.utils import get_resource_state
from waldur_mastermind.marketplace_rancher import PLUGIN_NAME

logger = logging.getLogger(__name__)


def create_marketplace_resource_for_imported_cluster(
    sender, instance, offering=None, plan=None, **kwargs
):
    if not offering or not plan:
        # When cluster is imported directly (ie without marketplace),
        # marketplace resources are not created.
        return
    resource = marketplace_models.Resource(
        project=instance.service_project_link.project,
        state=get_resource_state(instance.state),
        name=instance.name,
        scope=instance,
        created=instance.created,
        plan=plan,
        offering=offering,
    )

    resource.init_cost()
    resource.save()
    resource.init_quotas()


def update_node_usage(sender, instance, created=False, **kwargs):
    if not instance.tracker.has_changed('state'):
        return

    cluster = instance.cluster

    try:
        resource = marketplace_models.Resource.objects.get(scope=cluster)
    except django_exceptions.ObjectDoesNotExist:
        logger.debug(
            'Skipping node usage synchronization because this '
            'marketplace.Resource does not exist.'
            'Cluster ID: %s',
            cluster.id,
        )
        return

    date = datetime.date.today()
    usage = cluster.node_set.filter(state=core_models.StateMixin.States.OK).count()

    resource.current_usages = {'nodes': usage}
    resource.save(update_fields=['current_usages'])

    for component in manager.get_components(PLUGIN_NAME):
        try:
            offering_component = marketplace_models.OfferingComponent.objects.get(
                offering=resource.offering, type=component.type
            )
            plan_period = (
                marketplace_models.ResourcePlanPeriod.objects.filter(
                    Q(start__lte=date) | Q(start__isnull=True)
                )
                .filter(Q(end__gt=date) | Q(end__isnull=True))
                .get(resource=resource)
            )

            try:
                component_usage = marketplace_models.ComponentUsage.objects.get(
                    resource=resource,
                    component=offering_component,
                    billing_period=month_start(date),
                    plan_period=plan_period,
                )
                component_usage.usage = max(usage, component_usage.usage)
                component_usage.save()
            except django_exceptions.ObjectDoesNotExist:
                marketplace_models.ComponentUsage.objects.create(
                    resource=resource,
                    component=offering_component,
                    usage=usage,
                    date=date,
                    billing_period=month_start(date),
                    plan_period=plan_period,
                )

        except marketplace_models.OfferingComponent.DoesNotExist:
            logger.warning(
                'Skipping node usage synchronization because this '
                'marketplace.OfferingComponent does not exist.'
                'Cluster ID: %s',
                cluster.id,
            )
        except marketplace_models.ResourcePlanPeriod.DoesNotExist:
            logger.warning(
                'Skipping node usage synchronization because this '
                'marketplace.ResourcePlanPeriod does not exist.'
                'Cluster ID: %s',
                cluster.id,
            )
