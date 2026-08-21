import logging
from functools import lru_cache

import requests
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes import fields as ct_fields
from django.contrib.contenttypes import models as ct_models
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils.fields import AutoCreatedField
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.core.fields import JSONField, UUIDField
from waldur_core.core.managers import GenericKeyMixin
from waldur_core.core.utils import send_mail, validate_outbound_url
from waldur_core.logging.enums import ObservableObjectType
from waldur_core.logging.log import scrub_sensitive

logger = logging.getLogger(__name__)


class UuidMixin(models.Model):
    # There is circular dependency between logging and core applications.
    # Core models are loggable. So we cannot use UUID mixin here.

    class Meta:
        abstract = True

    uuid = UUIDField()


class EventTypesMixin(models.Model):
    """
    Mixin to add a event_types and event_groups fields.
    """

    class Meta:
        abstract = True

    event_types = models.JSONField("List of event types")
    event_groups = models.JSONField("List of event groups", default=list)

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]


class BaseHook(EventTypesMixin, UuidMixin, TimeStampedModel):
    class Meta:
        ordering = ["-created", "id"]

    user = models.ForeignKey[core_models.User](
        on_delete=models.CASCADE, to=settings.AUTH_USER_MODEL
    )
    is_active = models.BooleanField(default=True)

    # This timestamp would be updated periodically when event is sent via this hook
    last_published = models.DateTimeField(default=timezone.now)

    @property
    def all_event_types(self):
        from waldur_core.logging import event_logger

        self_types = set(self.event_types)
        try:
            hook_ct = ct_models.ContentType.objects.get_for_model(self)
            base_types = SystemNotification.objects.get(hook_content_type=hook_ct)
        except SystemNotification.DoesNotExist:
            return self_types
        else:
            return (
                self_types
                | set(event_logger.expand_event_groups(base_types.event_groups))
                | set(base_types.event_types)
            )

    @classmethod
    def get_active_hooks(cls):
        return [
            obj
            for hook in cls.__subclasses__()
            for obj in hook.objects.filter(is_active=True)
        ]

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]

    @classmethod
    def get_all_content_types(cls):
        ctypes = ct_models.ContentType.objects.get_for_models(*cls.get_all_models())
        ids = [ctype.id for ctype in ctypes.values()]
        return ct_models.ContentType.objects.filter(id__in=ids)


class WebHook(BaseHook):
    class ContentTypeChoices:
        JSON = 1
        FORM = 2
        CHOICES = ((JSON, "json"), (FORM, "form"))

    destination_url = models.URLField(validators=[validate_outbound_url])
    content_type = models.SmallIntegerField(
        choices=ContentTypeChoices.CHOICES, default=ContentTypeChoices.JSON
    )

    # Hard cap on outbound webhook latency. Operators can lower this via
    # settings.WEBHOOK_REQUEST_TIMEOUT (defaults applied below).
    _DEFAULT_TIMEOUT_SECONDS = 10

    def process(self, event):
        logger.debug(
            "Submitting web hook to URL %s, payload: %s", self.destination_url, event
        )
        # Re-validate at request time to defeat DNS rebinding between
        # validation (at save) and connect (here). Skip the call if the
        # destination is no longer safe.
        try:
            validate_outbound_url(self.destination_url)
        except DjangoValidationError as exc:
            logger.warning(
                "Skipping webhook %s — destination %s is no longer safe: %s",
                self.uuid,
                self.destination_url,
                exc,
            )
            return

        # Scrub credential-named keys from the context before it leaves for an
        # external URL — defence in depth against a secret ever reaching a context.
        payload = dict(
            created=event.created.isoformat(),
            message=event.message,
            context=scrub_sensitive(event.context),
            event_type=event.event_type,
        )

        timeout = getattr(
            settings, "WEBHOOK_REQUEST_TIMEOUT", self._DEFAULT_TIMEOUT_SECONDS
        )
        # `allow_redirects=False` prevents a public destination from
        # redirecting (302/307) to an internal address mid-flight.
        common_kwargs = dict(
            verify=settings.VERIFY_WEBHOOK_REQUESTS,
            timeout=timeout,
            allow_redirects=False,
        )

        if self.content_type == WebHook.ContentTypeChoices.JSON:
            requests.post(self.destination_url, json=payload, **common_kwargs)
        elif self.content_type == WebHook.ContentTypeChoices.FORM:
            requests.post(self.destination_url, data=payload, **common_kwargs)


class EmailHook(BaseHook):
    email = models.EmailField(max_length=320)

    def process(self, event):
        if not self.email:
            logger.info(
                "Skipping processing of email hook (PK=%s) because email is not defined"
                % self.pk
            )
            return
        subject = settings.WALDUR_CORE.get(
            "NOTIFICATION_SUBJECT", "Notifications from Waldur"
        )
        text_message = event.message
        html_message = render_to_string("logging/email.html", {"events": [event]})
        logger.info(
            "Submitting email hook to %s, payload: %s", self.email, text_message
        )
        if settings.EMAIL_HOOK_FROM_EMAIL:
            send_mail(
                subject,
                text_message,
                [self.email],
                html_message=html_message,
                from_email=settings.EMAIL_HOOK_FROM_EMAIL,
            )
        else:
            send_mail(
                subject,
                text_message,
                [self.email],
                html_message=html_message,
            )


class SystemNotification(EventTypesMixin, models.Model):
    # Model doesn't inherit NameMixin, because this is circular dependence.
    name = models.CharField(_("name"), max_length=150)
    hook_content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ct_models.ContentType, related_name="+"
    )
    roles = JSONField("List of roles", default=list)

    @staticmethod
    def get_valid_roles():
        return "admin", "manager", "owner"

    def __str__(self):
        return f"{self.hook_content_type} | {self.name}"


class Event(UuidMixin):
    created = AutoCreatedField(db_index=True)
    event_type = models.CharField(max_length=100, db_index=True)
    message = models.TextField()
    context = models.JSONField(blank=True)

    class Meta:
        ordering = ["-created", "id"]
        indexes = [
            models.Index(fields=["-created", "event_type"]),
        ]

    def __str__(self):
        return f"{self.event_type}: {self.message}"


class FeedManager(GenericKeyMixin, models.Manager):
    pass


class Feed(models.Model):
    event = models.ForeignKey(on_delete=models.CASCADE, to=Event)
    content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ct_models.ContentType, db_index=True
    )
    object_id = models.PositiveIntegerField(db_index=True)
    scope = ct_fields.GenericForeignKey("content_type", "object_id")
    objects = FeedManager()

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self):
        return f"{self.event} for {self.scope}"


# DEPRECATED: EventSubscription + EventSubscriptionQueue are the legacy
# per-(subscription x offering x object_type) queue model, superseded by the
# unified EventConsumer path (one consumer_{uuid} queue, demultiplexed on the
# envelope's object_type). Kept running untouched during the deprecation window.
# Removal is tracked in WAL-10111 and gated on drain telemetry, not a date.
# See "The legacy path" in docs/design/pubsub-architecture.md.
class EventSubscription(UuidMixin, TimeStampedModel, core_models.DescribableMixin):
    user = models.ForeignKey(to=core_models.User, on_delete=models.CASCADE)
    source_ip = models.GenericIPAddressField(protocol="IPv4", null=True, blank=True)
    # observable_objects is a list of dicts, where each dict contains:
    # object_type - string with model name of an observable object
    # object_uuid - UUID of an object; if None, the subscriber observes all the existing objects of object_type
    observable_objects = models.JSONField(
        default=list,
        help_text=_("List of observable objects"),
    )


class EventSubscriptionQueue(UuidMixin, TimeStampedModel):
    """Represents a RabbitMQ queue for an event subscription.

    This model tracks queues that have been explicitly created via the API.
    Receivers must create queues before subscribing via STOMP to ensure
    queues are created with correct arguments (DLX, max-length, etc.).
    """

    event_subscription = models.ForeignKey(
        EventSubscription,
        on_delete=models.CASCADE,
        related_name="queues",
    )
    offering_uuid = models.UUIDField(
        help_text=_("UUID of the offering this queue receives events for"),
    )
    object_type = models.CharField(
        max_length=50,
        help_text=_("Observable object type (e.g., 'resource', 'order')"),
    )

    class Meta:
        unique_together = ("event_subscription", "offering_uuid", "object_type")
        verbose_name = _("Subscription queue")
        ordering = ["-created", "id"]

    @property
    def queue_name(self) -> str:
        """Generate the RabbitMQ queue name."""
        return f"subscription_{self.event_subscription.uuid.hex}_offering_{self.offering_uuid.hex}_{self.object_type}"

    @property
    def vhost(self) -> str:
        """Get the RabbitMQ vhost (user UUID hex)."""
        return self.event_subscription.user.uuid.hex

    def __str__(self):
        return self.queue_name


def validate_observable_object_types(value):
    valid_values = {member.value for member in ObservableObjectType}
    for item in value:
        if item not in valid_values:
            raise DjangoValidationError(
                f"Invalid object type '{item}'. Valid types: {sorted(valid_values)}"
            )


class EventConsumer(UuidMixin, TimeStampedModel):
    """A generic pub/sub consumer draining a single unified queue.

    One RabbitMQ queue (``consumer_{uuid}``) per consumer. Scope is expressed as
    a *list* of entity bindings in :class:`EventConsumerScope` (PAT-style) —
    a consumer may be bound to several projects, a customer, an offering, etc.
    Bindings are GenericForeignKeys so ``waldur_core`` stays free of marketplace
    imports. Any external integration (a site agent, an IdM/IGA sync, the
    keycloak operator) owns an EventConsumer; the site-agent case links it from
    ``AgentIdentity.event_consumer``.

    **An empty ``scopes`` set means global (unrestricted) and is staff/support
    only** — it receives the all-user PII firehose, so a consumer must never be
    left bindingless by accident. Create the consumer and its bindings in one
    transaction.
    """

    user = models.ForeignKey(
        to=core_models.User,
        on_delete=models.CASCADE,
        related_name="event_consumers",
        help_text=_("Owner/registrant; the RabbitMQ vhost is the user UUID hex."),
    )
    rmq_username = models.CharField(
        max_length=32,
        blank=True,
        help_text=_("RabbitMQ username (UUID hex) for the consumer queue."),
    )
    queue_created = models.BooleanField(default=False)
    object_types = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "List of observable object types this consumer receives. "
            "Empty list means all types."
        ),
        validators=[validate_observable_object_types],
    )

    @property
    def queue_name(self) -> str:
        return f"consumer_{self.uuid.hex}"

    @property
    def vhost(self) -> str:
        return self.user.uuid.hex

    @property
    def is_global(self) -> bool:
        """No bindings = unrestricted (staff/support only)."""
        return not self.scopes.exists()

    def __str__(self):
        return f"EventConsumer {self.uuid.hex} (user={self.user_id})"


class EventConsumerScope(models.Model):
    """One entity binding of an :class:`EventConsumer`.

    Normalized rather than JSON (which is how ``PersonalAccessToken`` stores
    ``allowed_scopes``) because pub/sub fan-out is the inverse problem: one
    event is matched against *many* consumers, so the dispatcher needs an
    indexed ``(content_type, object_id)`` to intersect against the event's
    scope-keys.
    """

    consumer = models.ForeignKey(
        EventConsumer,
        on_delete=models.CASCADE,
        related_name="scopes",
    )
    content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ct_models.ContentType, db_index=True
    )
    object_id = models.PositiveIntegerField(db_index=True)
    scope = ct_fields.GenericForeignKey("content_type", "object_id")

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["consumer", "content_type", "object_id"],
                name="unique_event_consumer_scope",
            )
        ]

    def __str__(self):
        return f"{self.consumer} -> {self.scope}"


class EmailLog(UuidMixin):
    sent_at = models.DateTimeField(auto_now_add=True)
    subject = models.CharField(max_length=255)
    body = models.TextField()
    emails = ArrayField(models.EmailField())

    def __str__(self):
        return f"Email to {self.emails} at {self.sent_at}"

    class Meta:
        ordering = ["-sent_at", "id"]


class SystemLog(TimeStampedModel):
    """Stores system logs from API, Worker, and Beat processes for staff viewing."""

    class SourceChoices(models.TextChoices):
        API = "api", "API"
        WORKER = "worker", "Worker"
        BEAT = "beat", "Beat"

    class Meta:
        ordering = ["-created", "id"]
        indexes = [
            models.Index(
                fields=["-created", "source"], name="logging_syslog_created_idx"
            ),
            models.Index(
                fields=["source", "instance", "-created"],
                name="logging_syslog_instance_idx",
            ),
        ]

    source = models.CharField(max_length=10, choices=SourceChoices.choices)
    instance = models.CharField(
        max_length=255,
        help_text=_("Pod name (K8s) or container name (Docker)"),
    )
    level = models.CharField(max_length=10)
    level_number = models.PositiveSmallIntegerField()
    logger_name = models.CharField(max_length=255)
    message = models.TextField()
    context = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"[{self.level}] {self.source}/{self.instance}: {self.message[:50]}"


class UserDataAccessLog(UuidMixin):
    """
    Log of user profile data access events.

    Tracks when and by whom a user's profile data was accessed via the API.
    Used for GDPR compliance and audit purposes.
    """

    class AccessorType:
        STAFF = "staff"
        SUPPORT = "support"
        ORGANIZATION_MEMBER = "organization_member"
        SERVICE_PROVIDER = "service_provider"
        SELF = "self"

        CHOICES = (
            (STAFF, "Staff"),
            (SUPPORT, "Support"),
            (ORGANIZATION_MEMBER, "Organization member"),
            (SERVICE_PROVIDER, "Service provider"),
            (SELF, "Self"),
        )

    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="data_access_logs",
        help_text=_("The user whose data was accessed"),
    )
    accessor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="performed_data_accesses",
        help_text=_("The user who accessed the data"),
    )
    timestamp = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text=_("When the access occurred"),
    )
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text=_("IP address of the accessor"),
    )
    accessor_type = models.CharField(
        max_length=50,
        choices=AccessorType.CHOICES,
        help_text=_("Type of accessor"),
    )
    accessed_fields = models.JSONField(
        default=list,
        help_text=_("List of user profile fields that were accessed"),
    )
    context = models.JSONField(
        default=dict,
        help_text=_("Additional context (endpoint, offering UUID for providers, etc.)"),
    )

    class Meta:
        ordering = ["-timestamp", "id"]
        indexes = [
            models.Index(fields=["target_user", "-timestamp"]),
        ]
        verbose_name = _("User data access log")
        verbose_name_plural = _("User data access logs")

    def __str__(self):
        accessor_name = self.accessor.username if self.accessor else "Unknown"
        return f"{accessor_name} accessed {self.target_user.username} data at {self.timestamp}"
