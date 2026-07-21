import hashlib
import json
import logging
import re
import threading
import time
import uuid as uuid_mod

import stomp
from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db.models import QuerySet
from rest_framework.exceptions import ValidationError

from waldur_core.logging import backend, models
from waldur_core.logging.circuit_breaker import stomp_circuit_breaker
from waldur_core.logging.mixins import LoggableMixin

logger = logging.getLogger(__name__)


class PublishingMetrics:
    """Track publishing metrics for monitoring and debugging.

    Thread-safe metrics collection for pubsub system observability.
    """

    messages_sent = 0
    messages_failed = 0
    messages_retried = 0
    messages_skipped = 0  # Circuit breaker skips
    circuit_breaker_trips = 0
    rate_limiter_rejections = 0
    _total_publish_time_ms = 0.0
    _publish_count = 0
    _last_publish_time: float | None = None
    _lock = threading.Lock()

    @classmethod
    def record_publish(cls, success: bool, duration_ms: float) -> None:
        """Record a publish attempt."""
        with cls._lock:
            cls._last_publish_time = time.time()
            cls._total_publish_time_ms += duration_ms
            cls._publish_count += 1
            if success:
                cls.messages_sent += 1
            else:
                cls.messages_failed += 1

    @classmethod
    def record_skip(cls) -> None:
        """Record a message skipped due to circuit breaker."""
        with cls._lock:
            cls.messages_skipped += 1

    @classmethod
    def record_circuit_breaker_trip(cls) -> None:
        """Record a circuit breaker trip."""
        with cls._lock:
            cls.circuit_breaker_trips += 1

    @classmethod
    def record_rate_limit_rejection(cls) -> None:
        """Record a rate limiter rejection."""
        with cls._lock:
            cls.rate_limiter_rejections += 1

    @classmethod
    def get_metrics(cls) -> dict:
        """Return all metrics as a dictionary."""
        with cls._lock:
            avg_publish_time = 0.0
            if cls._publish_count > 0:
                avg_publish_time = cls._total_publish_time_ms / cls._publish_count
            return {
                "messages_sent": cls.messages_sent,
                "messages_failed": cls.messages_failed,
                "messages_retried": cls.messages_retried,
                "messages_skipped": cls.messages_skipped,
                "circuit_breaker_trips": cls.circuit_breaker_trips,
                "rate_limiter_rejections": cls.rate_limiter_rejections,
                "avg_publish_time_ms": round(avg_publish_time, 2),
                "last_publish_time": cls._last_publish_time,
            }

    @classmethod
    def reset(cls) -> None:
        """Reset all metrics to zero."""
        with cls._lock:
            cls.messages_sent = 0
            cls.messages_failed = 0
            cls.messages_retried = 0
            cls.messages_skipped = 0
            cls.circuit_breaker_trips = 0
            cls.rate_limiter_rejections = 0
            cls._total_publish_time_ms = 0.0
            cls._publish_count = 0
            cls._last_publish_time = None


class RateLimiter:
    """Token bucket rate limiter for message publishing.

    Prevents burst publishing that could overwhelm RabbitMQ.
    """

    def __init__(self, rate: float = 500.0, burst: int = 1000):
        """Initialize rate limiter.

        Args:
            rate: Messages per second allowed
            burst: Maximum burst size (tokens available at start)
        """
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last_update = time.time()
        self._lock = threading.Lock()

    def acquire(self, count: int = 1) -> bool:
        """Try to acquire tokens for publishing.

        Args:
            count: Number of tokens to acquire

        Returns:
            True if tokens acquired, False if rate limited
        """
        with self._lock:
            now = time.time()
            elapsed = now - self._last_update
            self._last_update = now

            # Replenish tokens based on elapsed time
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)

            if self._tokens >= count:
                self._tokens -= count
                return True
            return False


class MessageStateTracker:
    """Track message state to avoid redundant sends.

    Uses Django cache to track the hash of last-sent message content
    for each resource/message_type combination. If content hasn't changed,
    the message is skipped.
    """

    CACHE_TTL = 3600 * 4  # 4 hours - longer than typical beat interval

    @classmethod
    def _get_cache_key(cls, resource_uuid: str, message_type: str) -> str:
        return f"msg_state:{message_type}:{resource_uuid}"

    @classmethod
    def _hash_payload(cls, payload: dict) -> str:
        """Create hash of payload, excluding volatile fields."""
        # Exclude timestamp and other non-content fields
        content = {
            k: v
            for k, v in payload.items()
            if k not in ("timestamp", "message_id", "sequence_number")
        }
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

    @classmethod
    def should_send_message(
        cls, resource_uuid: str, message_type: str, payload: dict
    ) -> bool:
        """Check if message should be sent based on content change.

        Returns:
            True if content has changed since last send, False otherwise.
        """
        cache_key = cls._get_cache_key(resource_uuid, message_type)
        current_hash = cls._hash_payload(payload)

        last_hash = cache.get(cache_key)
        if last_hash == current_hash:
            logger.debug(
                "Skipping redundant message for %s (hash unchanged)", resource_uuid
            )
            return False

        # Update cache with new hash
        cache.set(cache_key, current_hash, cls.CACHE_TTL)
        return True

    @classmethod
    def get_cache_stats(
        cls, resource_uuid: str | None = None, message_type: str | None = None
    ) -> dict:
        """Get cache statistics for debugging.

        Note: Full cache inspection requires Redis backend with KEYS support.
        """
        return {
            "cache_ttl": cls.CACHE_TTL,
            "description": "State cache prevents duplicate message sends",
            "filter": {
                "resource_uuid": resource_uuid,
                "message_type": message_type,
            },
        }


def get_next_sequence_number(resource_uuid: str, message_type: str) -> int:
    """Get monotonically increasing sequence number for message ordering.

    Enables consumers to detect out-of-order or duplicate messages.

    Args:
        resource_uuid: UUID of the resource
        message_type: Type of message (e.g., 'resource', 'order')

    Returns:
        Next sequence number for this resource/type combination
    """
    cache_key = f"msg_seq:{message_type}:{resource_uuid}"

    try:
        seq = cache.incr(cache_key)
    except ValueError:
        # Key doesn't exist, initialize
        cache.set(cache_key, 1, timeout=3600 * 24)  # 24 hours
        seq = 1

    return seq


def create_message_id(message_info: dict) -> str:
    """Create deterministic message ID for deduplication.

    Args:
        message_info: Message dictionary with vhost, topic, payload

    Returns:
        32-character hex string ID
    """
    content = (
        f"{message_info['vhost']}:{message_info['topic']}:{message_info['payload']}"
    )
    return hashlib.sha256(content.encode()).hexdigest()[:32]


def get_message_ttl_for_type(message_type: str) -> int:
    """Get appropriate TTL in milliseconds based on message type.

    TTL should be slightly longer than beat interval to allow processing
    but short enough that stale messages expire.
    """
    TTL_MAP = {
        "periodic_limits": 3600 * 2,  # 2 hours
        "usage_update": 3600,  # 1 hour
        "order": 3600 * 24,  # 24 hours (important)
        "user_role": 3600 * 12,  # 12 hours
        "resource": 3600 * 2,  # 2 hours
    }
    return TTL_MAP.get(message_type, 3600) * 1000  # Convert to milliseconds


# Global rate limiter for STOMP publishing
_stomp_rate_limiter = RateLimiter(rate=500.0, burst=1000)


def parse_subscription_queue_name(queue_name: str) -> dict | None:
    """Parse a subscription queue name into its components.

    Queue names follow the pattern:
        subscription_{subscription_uuid}_offering_{offering_uuid}_{object_type}

    Args:
        queue_name (str): The queue name to parse

    Returns:
        dict | None: Dictionary with parsed components or None if not a valid subscription queue:
            - subscription_uuid: The subscription UUID
            - offering_uuid: The offering UUID
            - object_type: The object type (e.g., 'resource', 'order')
    """
    pattern = r"^subscription_([a-f0-9]+)_offering_([a-f0-9]+)_(\w+)$"
    match = re.match(pattern, queue_name)
    if match:
        return {
            "subscription_uuid": match.group(1),
            "offering_uuid": match.group(2),
            "object_type": match.group(3),
        }
    return None


def parse_consumer_queue_name(queue_name: str) -> str | None:
    """Parse a consumer queue name and return the EventConsumer UUID.

    Queue names follow the pattern: consumer_{event_consumer_uuid}

    Args:
        queue_name: The queue name to parse

    Returns:
        The event_consumer_uuid string or None if not a valid consumer queue.
    """
    pattern = r"^consumer_([a-f0-9]{32})$"
    match = re.match(pattern, queue_name)
    if match:
        return match.group(1)
    return None


def resolve_consumer_rmq_password(request) -> str:
    """RabbitMQ password for a consumer queue.

    Uses the presented Personal Access Token when the caller authenticated with
    one (the client presents the same PAT string as its STOMP passcode, and a
    long-lived PAT survives a session-token logout/rotation); otherwise falls
    back to the get-or-created DRF token (avoiding the Token.DoesNotExist a bare
    ``request.user.auth_token`` would raise). No hard gate. cleanup_stale is
    PAT-aware to match.
    """
    # Kept lazy deliberately: importing waldur_core.core.authentication /
    # core.models at this module's top raises AppRegistryNotReady, because
    # logging.utils is imported early during app loading (before the core app's
    # models are ready). Not the optional-backend-SDK carve-out, but the same
    # app-initialization-order hazard the CLAUDE.md rule exists to avoid.
    from waldur_core.core.authentication import (
        parse_token_from_request,
        refresh_token,
    )
    from waldur_core.core.models import PersonalAccessToken

    if isinstance(request.auth, PersonalAccessToken):
        raw_pat = parse_token_from_request(request, b"bearer")
        if raw_pat:
            return raw_pat
    return refresh_token(request.user).key


def provision_consumer_queue(consumer, password: str) -> dict:
    """Create the RMQ vhost/user/queue for an EventConsumer and mark it created.

    These are external, non-transactional side effects and must run OUTSIDE any
    DB transaction. Raises rest_framework ValidationError on failure, tearing
    down a just-created user first. Idempotency / ownership / stale-recreate
    stay with the caller; this is only the fresh-provision step.
    """
    vhost = consumer.user.uuid.hex
    queue_name = consumer.queue_name
    rmq_backend = backend.RabbitMQManagementBackend()
    new_rmq_username = uuid_mod.uuid4().hex

    if not rmq_backend.create_rabbitmq_virtual_host(vhost):
        logger.error("Failed to create RabbitMQ virtual host: %s", vhost)
        raise ValidationError("Failed to create RabbitMQ virtual host")
    if not rmq_backend.create_rabbitmq_user(new_rmq_username, password):
        logger.error("Failed to create RabbitMQ user: %s", new_rmq_username)
        raise ValidationError("Failed to create RabbitMQ user")
    permissions = {"configure": ".*", "write": ".*", "read": ".*"}
    if not rmq_backend.assign_rabbitmq_vhost_permissions(
        new_rmq_username, vhost, permissions
    ):
        logger.error(
            "Failed to assign RabbitMQ permissions for user: %s", new_rmq_username
        )
        rmq_backend.delete_rabbitmq_user(new_rmq_username)
        raise ValidationError("Failed to assign RabbitMQ permissions")
    # arguments= must be a keyword — see the site-agent register_queue note.
    if not rmq_backend.create_queue(
        vhost, queue_name, arguments=backend.SUBSCRIPTION_QUEUE_ARGUMENTS
    ):
        logger.error("Failed to create RabbitMQ queue: %s", queue_name)
        rmq_backend.delete_rabbitmq_user(new_rmq_username)
        raise ValidationError("Failed to create RabbitMQ queue")

    consumer.rmq_username = new_rmq_username
    consumer.queue_created = True
    consumer.save(update_fields=["rmq_username", "queue_created"])
    return {
        "rmq_username": new_rmq_username,
        "queue_name": queue_name,
        "vhost": vhost,
    }


# Advisory-lock namespace for consumer registration (arbitrary constant, "EVNT").
_REGISTRATION_LOCK_NAMESPACE = 0x45564E54


def lock_user_registration(user_id: int) -> None:
    """Serialize a user's concurrent consumer registrations within the current
    transaction (must be called inside ``transaction.atomic()``).

    ``select_for_update`` cannot lock rows that do not exist yet, so two
    concurrent FIRST-time registrations both see an empty candidate set and both
    insert — creating duplicate consumers + RMQ queues. A per-user Postgres
    transaction-level advisory lock covers first-time AND re-registration; it is
    released automatically at transaction end. No-op on non-PostgreSQL backends.
    """
    from django.db import connection

    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_REGISTRATION_LOCK_NAMESPACE, user_id],
        )


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


# Throttle for STOMP publish-failure ERROR logs. During a sustained RabbitMQ
# outage the same connection error fires for every message and every Celery
# retry, burying actionable signal under thousands of duplicate tracebacks.
# Emit at most one full traceback per minute per process; downgrade the rest
# to a one-line WARNING.
_STOMP_FAILURE_ERROR_LOG_INTERVAL_S = 60.0
_stomp_failure_log_lock = threading.Lock()
_stomp_failure_last_error_log_time: float = 0.0


def _log_stomp_publish_failure(exc: BaseException) -> None:
    """Log a STOMP publish failure with bounded frequency.

    First failure (or one per ``_STOMP_FAILURE_ERROR_LOG_INTERVAL_S``) is
    logged at ERROR with a traceback so on-call sees the root cause.
    Subsequent failures within the window are logged at WARNING without a
    traceback so a long outage doesn't drown the error stream.
    """
    global _stomp_failure_last_error_log_time
    now = time.monotonic()
    with _stomp_failure_log_lock:
        emit_error = (
            now - _stomp_failure_last_error_log_time
            >= _STOMP_FAILURE_ERROR_LOG_INTERVAL_S
        )
        if emit_error:
            _stomp_failure_last_error_log_time = now

    if emit_error:
        logger.exception("Failed to publish message to RabbitMQ STOMP queue: %s", exc)
    else:
        logger.warning(
            "STOMP publish failed (traceback suppressed; one ERROR per %ss): %s",
            int(_STOMP_FAILURE_ERROR_LOG_INTERVAL_S),
            exc,
        )


def publish_stomp_messages(
    messages_to_send: list[dict[str, str]],
) -> tuple[int, int]:
    """Publish messages to RabbitMQ via STOMP protocol.

    Includes circuit breaker protection, rate limiting, and metrics tracking.

    Args:
        messages_to_send: List of message dictionaries with vhost, topic, payload

    Returns:
        Tuple of (successful_count, failed_count)
    """
    rabbitmq_settings: dict = settings.RABBITMQ
    if not rabbitmq_settings.get("STOMP_PORT"):
        logger.warning("STOMP_PORT is not defined in settings")
        return (0, len(messages_to_send))

    # Check circuit breaker (use can_execute() so recovery timeout is respected)
    if not stomp_circuit_breaker.can_execute():
        logger.warning(
            "STOMP circuit breaker is OPEN, skipping %d messages",
            len(messages_to_send),
        )
        for _ in messages_to_send:
            PublishingMetrics.record_skip()
        return (0, len(messages_to_send))

    host = rabbitmq_settings["HOST"]
    port = rabbitmq_settings["STOMP_PORT"]
    username = rabbitmq_settings["USER"]
    password = rabbitmq_settings["PASSWORD"]

    successful = 0
    failed = 0

    for message_info in messages_to_send:
        # Check rate limiter
        if not _stomp_rate_limiter.acquire():
            logger.warning("Rate limit exceeded, skipping message")
            PublishingMetrics.record_rate_limit_rejection()
            failed += 1
            continue

        # Check circuit breaker before each message (it might have tripped)
        if not stomp_circuit_breaker.can_execute():
            logger.warning("Circuit breaker tripped during batch, skipping message")
            PublishingMetrics.record_skip()
            failed += 1
            continue

        connection = None
        start_time = time.time()
        try:
            connection = stomp.Connection12(
                host_and_ports=[(host, port)],
                vhost=message_info["vhost"],
                heartbeats=(10000, 10000),  # 10 second heartbeat
            )
            connection.connect(username=username, passcode=password, wait=True)
            # Use /amq/queue/ destination to send to pre-declared queues without
            # attempting to re-declare them. This avoids PRECONDITION_FAILED errors
            # when queues were created with specific arguments (e.g., x-message-ttl).
            # See: https://www.rabbitmq.com/docs/stomp#d.aqd
            queue_name = message_info["topic"].replace("/", "_")
            destination = "/amq/queue/" + queue_name

            headers = {
                "persistent": "true",
                "durable": "true",
                "auto-delete": "false",
                "content-type": "application/json",
                "x-message-id": create_message_id(message_info),
            }
            logger.info(
                "Sending STOMP message %s to %s", message_info["payload"], destination
            )
            connection.send(
                destination=destination,
                body=message_info["payload"],
                headers=headers,
            )
            stomp_circuit_breaker.record_success()
            successful += 1
            duration_ms = (time.time() - start_time) * 1000
            PublishingMetrics.record_publish(success=True, duration_ms=duration_ms)
        except Exception as e:
            _log_stomp_publish_failure(e)
            was_open = stomp_circuit_breaker.is_open()
            stomp_circuit_breaker.record_failure()
            failed += 1
            duration_ms = (time.time() - start_time) * 1000
            PublishingMetrics.record_publish(success=False, duration_ms=duration_ms)

            # Check if this failure tripped the circuit breaker. The state
            # transition itself is already logged at INFO by CircuitBreaker;
            # only record the metric here.
            if not was_open and stomp_circuit_breaker.is_open():
                PublishingMetrics.record_circuit_breaker_trip()
                logger.warning(
                    "Circuit breaker tripped after failure, remaining messages will be skipped"
                )
        finally:
            if connection and connection.is_connected():
                connection.disconnect()

    return (successful, failed)
