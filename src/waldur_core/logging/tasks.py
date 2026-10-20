import logging
from collections import defaultdict
from datetime import timedelta

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q, QuerySet
from django.utils import timezone

from waldur_core.configuration import config
from waldur_core.core.models import User
from waldur_core.logging import backend, models, utils
from waldur_core.logging.circuit_breaker import stomp_circuit_breaker
from waldur_core.logging.event_logger import get_event_groups
from waldur_core.logging.models import BaseHook, Event, Feed, UserDataAccessLog
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
            users = users_qs[0].union(*users_qs[1:])
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
    if not utils.can_view_event(hook.user, event):
        return False

    # Check permissions: the hook owner must be able to read the scope and
    # its event feed, exactly as through the events API.
    for feed in Feed.objects.filter(event=event):
        qs = feed.content_type.model_class().get_permitted_objects(hook.user)
        if not qs.filter(id=feed.object_id).exists():
            continue
        if feed.scope is not None and not utils.can_view_scope_events(
            hook.user, feed.scope
        ):
            continue
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


@shared_task(
    name="waldur_core.logging.tasks.publish_messages",
    bind=True,
    autoretry_for=(ConnectionError, OSError, Exception),
    retry_backoff=True,
    retry_backoff_max=300,  # Max 5 minutes between retries
    max_retries=5,
    retry_jitter=True,  # Add randomness to prevent thundering herd
)
def publish_messages(self, messages: list[dict[str, str]]) -> dict:
    """Publish messages to STOMP message queue.

    Uses Celery's built-in retry mechanism with exponential backoff.
    Returns statistics about successful and failed message delivery.
    """
    stomp_messages, webhook_payloads = _split_by_transport(messages)
    if webhook_payloads:
        # Each webhook consumer gets its own delivery task, so a dead listener
        # retries on its own schedule. The STOMP half is re-queued as a task of
        # its own rather than published here: this task retries with its
        # original arguments, and must not hand the webhooks out a second time.
        for consumer_uuid, payloads in webhook_payloads.items():
            deliver_webhook_messages.delay(consumer_uuid, payloads)
        if stomp_messages:
            publish_messages.delay(stomp_messages)
        return {
            "stomp": {"sent": 0, "failed": 0},
            "webhook_consumers": len(webhook_payloads),
            "retry_count": self.request.retries,
        }

    results = {
        "stomp": {"sent": 0, "failed": 0},
        "retry_count": self.request.retries,
    }

    # STOMP publishing. The underlying call already logs failures (rate-limited
    # to keep sustained outages from flooding the error stream) and updates the
    # circuit breaker. Don't re-log the same condition here — Celery's
    # task_retry / task_failed log will capture the retry path on its own.
    successful, failed = utils.publish_stomp_messages(messages)
    results["stomp"]["sent"] = successful
    results["stomp"]["failed"] = failed

    # If all STOMP messages failed and the circuit breaker hasn't already
    # tripped, raise so Celery retries this task. Once the breaker is OPEN we
    # accept the loss for this batch — Celery retrying would just keep hitting
    # the same dead backend.
    if failed > 0 and successful == 0 and not stomp_circuit_breaker.is_open():
        raise ConnectionError(f"All {failed} STOMP messages failed to publish")

    return results


def _split_by_transport(messages):
    """STOMP messages as a list, webhook payloads grouped by consumer uuid."""
    stomp_messages = []
    webhook_payloads = defaultdict(list)
    for message in messages:
        consumer_uuid = message.get("webhook_consumer")
        if consumer_uuid:
            webhook_payloads[consumer_uuid].append(message["payload"])
        else:
            stomp_messages.append(message)
    return stomp_messages, dict(webhook_payloads)


WEBHOOK_MAX_RETRIES = 8
WEBHOOK_RETRY_BACKOFF_MAX = 600


@shared_task(
    name="waldur_core.logging.deliver_webhook_messages",
    bind=True,
    max_retries=WEBHOOK_MAX_RETRIES,
    # Off the default queue: a slow or unreachable listener must not starve
    # every other task of workers.
    is_background=True,
)
def deliver_webhook_messages(self, consumer_uuid: str, payloads: list[str]) -> dict:
    """POST each payload of one batch, in order, to a webhook consumer.

    Delivery is at-least-once, and ordered only within this batch: every event
    is its own batch and retries on its own schedule, so two events can arrive
    in either order. Messages carry ``emitted_at`` and an
    ``X-Waldur-Message-Id`` header so the receiver can reorder and deduplicate.

    On the first failure the task retries with the payloads not yet delivered,
    with exponential backoff. Once the retries are exhausted the remainder is
    dropped and logged; ``resync`` is how a receiver recovers the full state.
    """
    consumer = (
        models.EventConsumer.objects.filter(
            uuid=consumer_uuid, delivery=models.EventConsumer.Delivery.WEBHOOK
        )
        .exclude(webhook_url="")
        .select_related("user")
        .first()
    )
    if consumer is None or not _may_still_receive(consumer):
        # Deleted, switched away from webhook delivery, or its owner lost the
        # right to this feed since the messages were queued (retries can run
        # for half an hour after dispatch).
        return {"sent": 0, "dropped": len(payloads)}

    sent = 0
    for index, payload in enumerate(payloads):
        try:
            utils.post_consumer_webhook(
                consumer.webhook_url,
                payload,
                utils.webhook_message_id(consumer_uuid, payload),
            )
        except SoftTimeLimitExceeded:
            # A long batch against a slow listener: hand the rest to a fresh
            # task rather than lose it with this one.
            _record_webhook_delivery(consumer, sent)
            deliver_webhook_messages.delay(consumer_uuid, payloads[index:])
            return {"sent": sent, "requeued": len(payloads) - index}
        except utils.WebhookDeliveryError as exc:
            _record_webhook_delivery(consumer, sent, error=str(exc))
            remaining = payloads[index:]
            if self.request.retries >= self.max_retries:
                logger.error(
                    "Dropping %s webhook messages for event consumer %s after %s "
                    "retries: %s",
                    len(remaining),
                    consumer_uuid,
                    self.request.retries,
                    exc,
                )
                return {"sent": sent, "dropped": len(remaining)}
            countdown = min(WEBHOOK_RETRY_BACKOFF_MAX, 10 * 2**self.request.retries)
            raise self.retry(args=[consumer_uuid, remaining], countdown=countdown)
        sent += 1

    _record_webhook_delivery(consumer, sent)
    return {"sent": sent, "dropped": 0}


def _may_still_receive(consumer) -> bool:
    """Delivery-time re-check of the owner.

    Only staff can create a webhook consumer, and the feed leaves the platform
    for a third party, so it stops as soon as its owner is no longer active
    staff or support -- bound or global alike.
    """
    user = consumer.user
    return user.is_active and (user.is_staff or user.is_support)


def _record_webhook_delivery(consumer, sent: int, error: str | None = None) -> None:
    # A queryset update, not save(): delivery tasks for one consumer may run
    # concurrently with an API edit of the same row.
    now = timezone.now()
    fields = {}
    if sent:
        fields["last_delivered_at"] = now
    if error is not None:
        fields["last_error"] = error
        fields["last_error_at"] = now
    if fields:
        models.EventConsumer.objects.filter(pk=consumer.pk).update(**fields)


@shared_task(name="waldur_core.logging.cleanup_orphan_subscription_queues")
def cleanup_orphan_subscription_queues() -> None:
    """Delete RabbitMQ subscription queues that have no matching DB record.

    This handles cases where:
    - The pre_delete signal failed to clean up a queue
    - DB records were deleted manually without triggering signals
    - Data corruption left orphaned queues in RabbitMQ

    Handles both subscription_ and consumer_ prefixed queues.
    """
    rmq_backend = backend.RabbitMQManagementBackend()
    all_vhost_data = rmq_backend.list_all_subscription_queues()
    deleted_count = 0

    for vhost_info in all_vhost_data:
        vhost = vhost_info["vhost"]
        for queue_info in vhost_info["queues"]:
            queue_name = queue_info["name"]

            # Handle consumer_ queues (unified EventConsumer path)
            if queue_name.startswith("consumer_"):
                consumer_uuid = utils.parse_consumer_queue_name(queue_name)
                if consumer_uuid:
                    has_consumer = models.EventConsumer.objects.filter(
                        uuid=consumer_uuid,
                        queue_created=True,
                    ).exists()
                    if has_consumer:
                        continue
                # Orphan consumer queue
                logger.info(
                    "Deleting orphan consumer queue %s in vhost %s",
                    queue_name,
                    vhost,
                )
                try:
                    rmq_backend.delete_queue(vhost, queue_name)
                    deleted_count += 1
                except Exception as e:
                    logger.warning(
                        "Failed to delete orphan queue %s in vhost %s: %s",
                        queue_name,
                        vhost,
                        e,
                    )
                continue

            # Handle subscription_ queues (existing logic)
            has_db_record = models.EventSubscriptionQueue.objects.filter(
                event_subscription__user__uuid=vhost,
                offering_uuid__isnull=False,
            ).exists()

            if has_db_record:
                matching = any(
                    q.queue_name == queue_name
                    for q in models.EventSubscriptionQueue.objects.filter(
                        event_subscription__user__uuid=vhost
                    )
                )
                if matching:
                    continue

            logger.info(
                "Deleting orphan subscription queue %s in vhost %s",
                queue_name,
                vhost,
            )
            try:
                rmq_backend.delete_queue(vhost, queue_name)
                deleted_count += 1
            except Exception as e:
                logger.warning(
                    "Failed to delete orphan queue %s in vhost %s: %s",
                    queue_name,
                    vhost,
                    e,
                )

    if deleted_count:
        logger.info("Cleaned up %d orphan subscription queues", deleted_count)


@shared_task(name="waldur_core.logging.log_user_data_access")
def log_user_data_access(
    target_user_uuid: str,
    accessor_uuid: str,
    accessor_type: str,
    accessed_fields: list[str],
    ip_address: str | None = None,
    context: dict | None = None,
) -> dict:
    """
    Asynchronously log user data access event.

    Args:
        target_user_uuid: UUID of the user whose data was accessed
        accessor_uuid: UUID of the user who accessed the data
        accessor_type: Type of accessor (staff, support, organization_member, self)
        accessed_fields: List of field names that were accessed
        ip_address: IP address of the accessor (optional)
        context: Additional context dict (endpoint, offering UUID, etc.)

    Returns:
        dict with log entry UUID or error message
    """
    # Check if logging is enabled
    if not config.USER_DATA_ACCESS_LOGGING_ENABLED:
        return {"status": "skipped", "reason": "logging_disabled"}

    # Skip self-access if configured
    if (
        target_user_uuid == accessor_uuid
        and not config.USER_DATA_ACCESS_LOG_SELF_ACCESS
    ):
        return {"status": "skipped", "reason": "self_access_not_logged"}

    try:
        target_user = User.objects.get(uuid=target_user_uuid)
        accessor = User.objects.get(uuid=accessor_uuid)

        log_entry = UserDataAccessLog.objects.create(
            target_user=target_user,
            accessor=accessor,
            accessor_type=accessor_type,
            accessed_fields=accessed_fields,
            ip_address=ip_address,
            context=context or {},
        )

        logger.debug(
            "Logged user data access: %s accessed %s data",
            accessor.username,
            target_user.username,
        )

        return {"status": "logged", "log_uuid": str(log_entry.uuid)}

    except User.DoesNotExist as e:
        logger.warning("User not found for data access log: %s", e)
        return {"status": "error", "reason": "user_not_found"}
    except Exception as e:
        logger.exception("Error logging user data access: %s", e)
        return {"status": "error", "reason": str(e)}


@shared_task(name="waldur_core.logging.cleanup_user_data_access_logs")
def cleanup_user_data_access_logs() -> dict:
    """
    Clean up old user data access logs based on retention period.

    Returns:
        dict with count of deleted records
    """
    retention_days = config.USER_DATA_ACCESS_LOG_RETENTION_DAYS
    cutoff_date = timezone.now() - timedelta(days=retention_days)

    deleted_count, _ = UserDataAccessLog.objects.filter(
        timestamp__lt=cutoff_date
    ).delete()

    logger.info(
        "Deleted %d user data access logs older than %d days",
        deleted_count,
        retention_days,
    )

    return {"deleted_count": deleted_count, "retention_days": retention_days}


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


@shared_task(name="waldur_core.logging.cleanup_system_logs")
def cleanup_system_logs():
    """
    Enforce row count limit per source (across all instances).

    Keeps newest logs, deletes oldest when count exceeds the configured limit.
    Runs periodically to maintain log volume within limits.
    """
    if not config.SYSTEM_LOG_ENABLED:
        return

    max_rows = config.SYSTEM_LOG_MAX_ROWS_PER_SOURCE

    for source in models.SystemLog.SourceChoices.values:
        total_count = models.SystemLog.objects.filter(source=source).count()
        delete_count = total_count - max_rows

        if delete_count > 0:
            # Find the cutoff: the created timestamp of the Nth oldest row to keep
            cutoff_row = (
                models.SystemLog.objects.filter(source=source)
                .order_by("-created")
                .values_list("created", flat=True)[max_rows : max_rows + 1]
            )
            if cutoff_row:
                deleted, _ = models.SystemLog.objects.filter(
                    source=source, created__lte=cutoff_row[0]
                ).delete()
                logger.info(
                    "Cleaned up %d system log entries for source %s (had %d rows, limit %d)",
                    deleted,
                    source,
                    total_count,
                    max_rows,
                )
