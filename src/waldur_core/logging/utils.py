import logging
from enum import Enum

from django.apps import apps
from django.conf import settings
from django.db.models import QuerySet

from paho.mqtt import publish as mqtt_publish
from waldur_core.logging import backend, models
from waldur_core.logging.loggers import LoggableMixin

logger = logging.getLogger(__name__)


class ObservableObjectType(Enum):
    ORDER = "order"
    USER_ROLE = "user_role"


def get_loggable_models():
    return [model for model in apps.get_models() if issubclass(model, LoggableMixin)]


def get_scope_types_mapping():
    return {str(m._meta): m for m in get_loggable_models()}


def get_reverse_scope_types_mapping():
    return {m: str(m._meta) for m in get_loggable_models()}


def delete_stale_subscriptions(
    stale_event_subscriptions: QuerySet[models.EventSubscription],
) -> QuerySet[models.EventSubscription]:
    rabbitmq_backend = backend.RabbitMQManagementBackend()
    removed_subscription_ids = []
    logger.info("Deleting users for stale event subscriptions")
    for subscription in stale_event_subscriptions:
        try:
            rabbitmq_backend.delete_rabbitmq_user(subscription.uuid)
            removed_subscription_ids.append(subscription.id)
        except Exception as e:
            logger.error(
                "Error deleting user for stale event subscription %s: %s",
                subscription.uuid,
                e,
            )
    return models.EventSubscription.objects.filter(id__in=removed_subscription_ids)


def publish_mqtt_messages(messages_to_send: list[dict[str, str]]) -> None:
    """Helper function to publish prepared MQTT messages"""
    mqtt_settings: dict = settings.RABBITMQ_MQTT

    for message_info in messages_to_send:
        try:
            logger.info(
                "Sending new message to mqtt://%s:%s, topic: %s",
                mqtt_settings["HOST"],
                mqtt_settings["PORT"],
                message_info["topic"],
            )
            mqtt_auth = {
                "username": f"{message_info['vhost']}:{mqtt_settings['USER']}",
                "password": mqtt_settings["PASSWORD"],
            }
            mqtt_publish.single(
                message_info["topic"],
                message_info["payload"],
                hostname=mqtt_settings["HOST"],
                port=mqtt_settings["PORT"],
                auth=mqtt_auth,
            )
        except Exception as exc:
            logger.exception(
                "Unable to send order info to mqtt://%s:%s, reason: %s",
                mqtt_settings["HOST"],
                mqtt_settings["PORT"],
                exc,
            )
