import logging

from celery import shared_task
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q, QuerySet

from waldur_core.core.models import User
from waldur_core.logging import backend, models, utils
from waldur_core.logging.event_logger import get_event_groups
from waldur_core.logging.models import BaseHook, Event, Feed
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import get_active_tokens

logger = logging.getLogger(__name__)


@shared_task(name="waldur_core.logging.process_event")
def process_event(event_id):
    event = Event.objects.get(id=event_id)
    for hook in get_matching_hooks(event):
        hook.process(event)

    process_system_notification(event)


def get_hooks(
    event_type,
    project: structure_models.Project | None = None,
    customer: structure_models.Customer | None = None,
):
    groups = [g[0] for g in get_event_groups().items() if event_type in g[1]]

    for hook in models.SystemNotification.objects.filter(
        Q(event_types__contains=event_type) | Q(event_groups__has_any_keys=groups)
    ):
        hook_class = hook.hook_content_type.model_class()
        users_qs: list[QuerySet[User]] = []

        if project:
            if "admin" in hook.roles:
                users_qs.append(get_users(project, RoleEnum.PROJECT_ADMIN))
            if "manager" in hook.roles:
                users_qs.append(get_users(project, RoleEnum.PROJECT_MANAGER))
            if "owner" in hook.roles:
                users_qs.append(get_users(project.customer, RoleEnum.CUSTOMER_OWNER))

        if customer:
            if "owner" in hook.roles:
                users_qs.append(get_users(customer, RoleEnum.CUSTOMER_OWNER))

        if len(users_qs) > 1:
            users = users_qs[0].union(*users_qs[1:]).distinct()
        elif len(users_qs) == 1:
            users = users_qs[0]
        else:
            users = []

        for user in users:
            if user.email:
                yield hook_class(
                    user=user, event_types=hook.event_types, email=user.email
                )


def process_system_notification(event: models.Event):
    project_ct = ContentType.objects.get_for_model(structure_models.Project)
    project_feed = Feed.objects.filter(event=event, content_type=project_ct).first()
    project = project_feed and project_feed.scope

    customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
    customer_feed = Feed.objects.filter(event=event, content_type=customer_ct).first()
    customer = customer_feed and customer_feed.scope

    for hook in get_hooks(event.event_type, project=project, customer=customer):
        if check_event(event, hook):
            hook.process(event)


def get_matching_hooks(event):
    """
    Returns a list of hooks that match the event.
    """
    active_hooks = BaseHook.get_active_hooks()
    return [hook for hook in active_hooks if check_event(event, hook)]


def check_event(event: models.Event, hook):
    # Check that event matches with hook
    if event.event_type not in hook.all_event_types:
        return False

    # Check permissions
    for feed in Feed.objects.filter(event=event):
        qs = feed.content_type.model_class().get_permitted_objects(hook.user)
        if qs.filter(id=feed.object_id).exists():
            return True

    return False


@shared_task(name="waldur_core.logging.delete_stale_event_subscriptions")
def delete_stale_event_subscriptions():
    active_tokens_list = get_active_tokens()
    stale_event_subscriptions = models.EventSubscription.objects.exclude(
        user__auth_token__in=active_tokens_list
    )

    logger.info(
        "Deleting %s event subscriptions for inactive sessions",
        stale_event_subscriptions.count(),
    )
    removed_subscriptions = utils.delete_stale_subscriptions(stale_event_subscriptions)
    removed_subscriptions.delete()


@shared_task(name="waldur_core.logging.tasks.publish_mqtt_messages")
def publish_messages(messages: list[dict[str, str]]) -> None:
    try:
        utils.publish_mqtt_messages(messages)
    except Exception as e:
        logger.error("Error publishing MQTT messages: %s", e)
    try:
        utils.publish_stomp_messages(messages)
    except Exception as e:
        logger.error("Error publishing STOMP messages: %s", e)


@shared_task(name="waldur_core.logging.delete_dangling_event_subscriptions")
def delete_dangling_event_subscriptions() -> None:
    event_subscriptions = models.EventSubscription.objects.all()
    rmq_backend = backend.RabbitMQManagementBackend()
    for event_subscription in event_subscriptions:
        try:
            rmq_username = event_subscription.uuid.hex
            rmq_user_info = rmq_backend.get_user(rmq_username)
        except Exception as exc:
            logger.exception("Unable to get user info from RabbitMQ, reason: %s", exc)
            continue
        if rmq_user_info is None:
            logger.info("Deleting event subscription %s", event_subscription.uuid)
            event_subscription.delete()
            continue
