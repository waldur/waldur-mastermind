import logging
from datetime import datetime
from typing import cast

from celery import shared_task

from waldur_core.core.utils import deserialize_instance, month_start
from waldur_mastermind.marketplace.enums import RANCHER_OFFERING, ResourceStates
from waldur_mastermind.marketplace.models import (
    ComponentUsage,
    OfferingComponent,
    Resource,
)
from waldur_mastermind.marketplace.utils import get_or_create_plan_period

from . import utils

logger = logging.getLogger(__name__)


@shared_task(name="waldur_mastermind.marketplace_rancher.report_rancher_usage")
def report_rancher_usage():
    for resource in Resource.objects.filter(
        offering__type=RANCHER_OFFERING, state=ResourceStates.OK
    ):
        collector = utils.UnifiedRancherUsageCollector()
        if not resource.scope:
            logger.debug(
                "Skipping usage collection for Rancher cluster %s because it is not found",
                resource.id,
            )
            continue
        usage = collector.collect_usage(resource.scope)
        today = datetime.today()

        for component_type, quantity in usage.items():
            offering_component = OfferingComponent.objects.get(
                offering=resource.offering, type=component_type
            )
            plan_period = get_or_create_plan_period(resource, today)
            ComponentUsage.objects.update_or_create(
                resource=resource,
                component=offering_component,
                billing_period=month_start(today),
                plan_period=plan_period,
                defaults={
                    "usage": quantity,
                    "date": today,
                },
            )


@shared_task(name="waldur_mastermind.marketplace_rancher.update_tenant_limits")
def update_tenant_limits(serialized_tenant_resource: str, new_limits: dict):
    tenant_resource = cast(Resource, deserialize_instance(serialized_tenant_resource))
    utils.submit_update_order(tenant_resource, new_limits)
