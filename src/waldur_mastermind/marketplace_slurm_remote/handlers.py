import json
import logging

from django.conf import settings
from django.core import exceptions as django_exceptions
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.utils import timezone

import paho.mqtt.publish as mqtt_publish
from waldur_core.core.utils import month_start
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME

logger = logging.getLogger(__name__)

COMPONENT_FIELDS = {
    "cpu_usage",
    "gpu_usage",
    "ram_usage",
    "cpu_limit",
    "gpu_limit",
    "ram_limit",
}


def update_component_quota(sender, instance, created=False, **kwargs):
    if created:
        return

    if not set(instance.tracker.changed()) & COMPONENT_FIELDS:
        return

    allocation = instance

    try:
        resource = marketplace_models.Resource.objects.get(scope=allocation)
    except django_exceptions.ObjectDoesNotExist:
        return

    for component in manager.get_components(PLUGIN_NAME):
        usage = getattr(allocation, component.type + "_usage")
        limit = getattr(allocation, component.type + "_limit")

        try:
            offering_component = marketplace_models.OfferingComponent.objects.get(
                offering=resource.offering, type=component.type
            )
        except marketplace_models.OfferingComponent.DoesNotExist:
            logger.warning(
                "Skipping Allocation synchronization because this "
                "marketplace.OfferingComponent does not exist."
                "Allocation ID: %s",
                allocation.id,
            )
        else:
            marketplace_models.ComponentQuota.objects.update_or_create(
                resource=resource,
                component=offering_component,
                defaults={"limit": limit, "usage": usage},
            )
            try:
                plan_period = marketplace_models.ResourcePlanPeriod.objects.get(
                    resource=resource, end=None
                )
            except (ObjectDoesNotExist, MultipleObjectsReturned):
                logger.warning(
                    "Skipping component usage synchronization because valid"
                    "ResourcePlanPeriod is not found."
                    "Allocation ID: %s",
                    allocation.id,
                )
            else:
                date = timezone.now()
                marketplace_models.ComponentUsage.objects.update_or_create(
                    resource=resource,
                    component=offering_component,
                    billing_period=month_start(date),
                    plan_period=plan_period,
                    defaults={"usage": usage, "date": date},
                )


def sync_component_user_usage_when_allocation_user_usage_is_submitted(
    sender, instance, **kwargs
):
    marketplace_utils.sync_component_user_usage(instance, PLUGIN_NAME)


def send_order_created_to_mqtt(sender, instance, created=False, **kwargs):
    if not settings.RABBITMQ_MQTT["ENABLED"]:
        return

    order: marketplace_models.Order = instance
    if not created:
        return

    if order.offering.type != PLUGIN_NAME:
        return

    topic_name = f"offering/{order.offering.uuid}/orders"

    mqtt_payload = json.dumps({"order_uuid": order.uuid.hex})
    mqtt_auth = {
        "username": settings.RABBITMQ_MQTT["USER"],
        "password": settings.RABBITMQ_MQTT["PASSWORD"],
    }

    try:
        logger.info(
            "Sending new order info %s to mqtt://%s:%s, topic: %s",
            order,
            settings.RABBITMQ_MQTT["HOST"],
            settings.RABBITMQ_MQTT["PORT"],
            topic_name,
        )
        mqtt_publish.single(
            topic_name,
            mqtt_payload,
            hostname=settings.RABBITMQ_MQTT["HOST"],
            port=settings.RABBITMQ_MQTT["PORT"],
            auth=mqtt_auth,
            retain=True,
        )
    except Exception as exc:
        logger.warning(
            "Unable to send order info %s to mqtt://%s:%s, reason: %s",
            order,
            settings.RABBITMQ_MQTT["HOST"],
            settings.RABBITMQ_MQTT["PORT"],
            exc,
        )
