import logging
from functools import lru_cache

import requests
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes import fields as ct_fields
from django.contrib.contenttypes import models as ct_models
from django.db import models
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils.fields import AutoCreatedField
from model_utils.models import TimeStampedModel

from waldur_core.core.fields import JSONField, UUIDField
from waldur_core.core.managers import GenericKeyMixin
from waldur_core.core.utils import send_mail
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users

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
        abstract = True
        ordering = ["-created"]

    user = models.ForeignKey(on_delete=models.CASCADE, to=settings.AUTH_USER_MODEL)
    is_active = models.BooleanField(default=True)

    # This timestamp would be updated periodically when event is sent via this hook
    last_published = models.DateTimeField(default=timezone.now)

    @property
    def all_event_types(self):
        from waldur_core.logging import loggers

        self_types = set(self.event_types)
        try:
            hook_ct = ct_models.ContentType.objects.get_for_model(self)
            base_types = SystemNotification.objects.get(hook_content_type=hook_ct)
        except SystemNotification.DoesNotExist:
            return self_types
        else:
            return (
                self_types
                | set(loggers.expand_event_groups(base_types.event_groups))
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

    destination_url = models.URLField()
    content_type = models.SmallIntegerField(
        choices=ContentTypeChoices.CHOICES, default=ContentTypeChoices.JSON
    )

    def process(self, event):
        logger.debug(
            "Submitting web hook to URL %s, payload: %s", self.destination_url, event
        )
        payload = dict(
            created=event.created.isoformat(),
            message=event.message,
            context=event.context,
            event_type=event.event_type,
        )

        # encode event as JSON
        if self.content_type == WebHook.ContentTypeChoices.JSON:
            requests.post(
                self.destination_url,
                json=payload,
                verify=settings.VERIFY_WEBHOOK_REQUESTS,
            )

        # encode event as form
        elif self.content_type == WebHook.ContentTypeChoices.FORM:
            requests.post(
                self.destination_url,
                data=payload,
                verify=settings.VERIFY_WEBHOOK_REQUESTS,
            )


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

    @classmethod
    def get_hooks(cls, event_type, project=None, customer=None):
        from waldur_core.logging import loggers

        groups = [
            g[0]
            for g in loggers.event_logger.get_all_groups().items()
            if event_type in g[1]
        ]

        for hook in cls.objects.filter(
            models.Q(event_types__contains=event_type)
            | models.Q(event_groups__has_any_keys=groups)
        ):
            hook_class = hook.hook_content_type.model_class()
            users_qs = []

            if project:
                if "admin" in hook.roles:
                    users_qs.append(get_users(project, RoleEnum.PROJECT_ADMIN))
                if "manager" in hook.roles:
                    users_qs.append(get_users(project, RoleEnum.PROJECT_MANAGER))
                if "owner" in hook.roles:
                    users_qs.append(
                        get_users(project.customer, RoleEnum.CUSTOMER_OWNER)
                    )

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

    def __str__(self):
        return f"{self.hook_content_type} | {self.name}"


class Event(UuidMixin):
    created = AutoCreatedField(db_index=True)
    event_type = models.CharField(max_length=100, db_index=True)
    message = models.TextField()
    context = models.JSONField(blank=True)

    class Meta:
        ordering = ("-created",)

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

    def __str__(self):
        return f"{self.event} for {self.scope}"
