import logging
from enum import Enum

import stomp
from django.apps import apps
from django.conf import settings
from django.db.models import QuerySet
from paho.mqtt import publish as mqtt_publish

from waldur_core.logging import backend, models
from waldur_core.logging.mixins import LoggableMixin

logger = logging.getLogger(__name__)


class ObservableObjectType(Enum):
    ORDER = "order"
    USER_ROLE = "user_role"
    RESOURCE = "resource"
    OFFERING_USER = "offering_user"
    IMPORTABLE_RESOURCES = "importable_resources"
    SERVICE_ACCOUNT = "service_account"
    COURSE_ACCOUNT = "course_account"


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
    mqtt_settings: dict = settings.RABBITMQ
    if not mqtt_settings.get("MQTT_PORT"):
        logger.warning("MQTT_PORT is not defined in settings")
        return

    for message_info in messages_to_send:
        try:
            logger.info(
                "Sending MQTT message to mqtt://%s:%s, topic: %s",
                mqtt_settings["HOST"],
                mqtt_settings["MQTT_PORT"],
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
                port=mqtt_settings["MQTT_PORT"],
                auth=mqtt_auth,
            )
        except Exception as exc:
            logger.exception(
                "Unable to send order info to mqtt://%s:%s, reason: %s",
                mqtt_settings["HOST"],
                mqtt_settings["MQTT_PORT"],
                exc,
            )


def publish_stomp_messages(messages_to_send: list[dict[str, str]]) -> None:
    """
    Publish messages to RabbitMQ via STOMP protocol.
    """
    rabbitmq_settings: dict = settings.RABBITMQ
    if not rabbitmq_settings.get("STOMP_PORT"):
        logger.warning("STOMP_PORT is not defined in settings")
        return

    host = rabbitmq_settings["HOST"]
    port = rabbitmq_settings["STOMP_PORT"]
    username = rabbitmq_settings["USER"]
    password = rabbitmq_settings["PASSWORD"]

    for message_info in messages_to_send:
        connection = None
        try:
            connection = stomp.Connection12(
                host_and_ports=[(host, port)],
                vhost=message_info["vhost"],
            )
            connection.connect(username=username, passcode=password, wait=True)
            destination = "/queue/" + message_info["topic"].replace("/", "_")
            headers = {
                "persistent": "true",  # Ensure message survival
                "durable": "true",  # Queue survives broker restart
                "auto-delete": "false",  # Queue exists even without consumers
                "content-type": "application/json",
            }
            logger.info(
                "Sending STOMP message %s to %s", message_info["payload"], destination
            )
            connection.send(
                destination=destination,
                body=message_info["payload"],
                headers=headers,
            )
        except Exception as e:
            logger.exception(
                "Failed to publish message to RabbitMQ STOMP queue: %s",
                e,
            )
        finally:
            if connection and connection.is_connected():
                connection.disconnect()
