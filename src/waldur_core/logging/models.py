from __future__ import unicode_literals

import logging
import uuid

import requests
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes import fields as ct_fields
from django.contrib.contenttypes import models as ct_models
from django.contrib.postgres.fields import JSONField as BetterJSONField
from django.core import validators
from django.core.mail import send_mail
from django.db import models
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.lru_cache import lru_cache
from django.utils.translation import ugettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core.fields import JSONField, UUIDField
from waldur_core.core.utils import timestamp_to_datetime
from waldur_core.logging import managers

logger = logging.getLogger(__name__)


class UuidMixin(models.Model):
    # There is circular dependency between logging and core applications.
    # Core models are loggable. So we cannot use UUID mixin here.

    class Meta:
        abstract = True

    uuid = UUIDField()


class Alert(UuidMixin, TimeStampedModel):
    class Meta:
        unique_together = ("content_type", "object_id", "alert_type", "is_closed")

    class SeverityChoices(object):
        DEBUG = 10
        INFO = 20
        WARNING = 30
        ERROR = 40
        CHOICES = ((DEBUG, 'Debug'), (INFO, 'Info'), (WARNING, 'Warning'), (ERROR, 'Error'))

    alert_type = models.CharField(max_length=50, db_index=True)
    message = models.CharField(max_length=255)
    severity = models.SmallIntegerField(choices=SeverityChoices.CHOICES)
    closed = models.DateTimeField(null=True, blank=True)
    # Hack: This field stays blank until alert closing.
    #       After closing it gets unique value to avoid unique together constraint break.
    is_closed = models.CharField(blank=True, max_length=32)
    acknowledged = models.BooleanField(default=False)
    context = JSONField(blank=True)

    content_type = models.ForeignKey(ct_models.ContentType, null=True, on_delete=models.SET_NULL)
    object_id = models.PositiveIntegerField(null=True)
    scope = ct_fields.GenericForeignKey('content_type', 'object_id')

    objects = managers.AlertManager()

    def close(self):
        self.closed = timezone.now()
        self.is_closed = uuid.uuid4().hex
        self.save()

    def acknowledge(self):
        self.acknowledged = True
        self.save()

    def cancel_acknowledgment(self):
        self.acknowledged = False
        self.save()


class AlertThresholdMixin(models.Model):
    """
    It is expected that model has scope field.
    """

    class Meta(object):
        abstract = True

    threshold = models.FloatField(default=0, validators=[validators.MinValueValidator(0)])

    def is_over_threshold(self):
        """
        If returned value is True, alert is generated.
        """
        raise NotImplementedError

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        from django.apps import apps
        return [model for model in apps.get_models() if issubclass(model, cls)]

    @classmethod
    def get_checkable_objects(cls):
        """
        It should return queryset of objects that should be checked.
        """
        return cls.objects.all()


class EventTypesMixin(models.Model):
    """
    Mixin to add a event_types and event_groups fields.
    """

    class Meta(object):
        abstract = True

    event_types = BetterJSONField('List of event types')
    event_groups = BetterJSONField('List of event groups', default=list)

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]


class BaseHook(EventTypesMixin, UuidMixin, TimeStampedModel):
    class Meta:
        abstract = True

    user = models.ForeignKey(settings.AUTH_USER_MODEL)
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
            return self_types | set(loggers.expand_event_groups(base_types.event_groups)) | set(base_types.event_types)

    @classmethod
    def get_active_hooks(cls):
        return [obj for hook in cls.__subclasses__() for obj in hook.objects.filter(is_active=True)]

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
    class ContentTypeChoices(object):
        JSON = 1
        FORM = 2
        CHOICES = ((JSON, 'json'), (FORM, 'form'))

    destination_url = models.URLField()
    content_type = models.SmallIntegerField(
        choices=ContentTypeChoices.CHOICES,
        default=ContentTypeChoices.JSON
    )

    def process(self, event):
        logger.debug('Submitting web hook to URL %s, payload: %s', self.destination_url, event)

        # encode event as JSON
        if self.content_type == WebHook.ContentTypeChoices.JSON:
            requests.post(self.destination_url, json=event, verify=settings.VERIFY_WEBHOOK_REQUESTS)

        # encode event as form
        elif self.content_type == WebHook.ContentTypeChoices.FORM:
            requests.post(self.destination_url, data=event, verify=settings.VERIFY_WEBHOOK_REQUESTS)


class PushHook(BaseHook):
    class Type:
        IOS = 1
        ANDROID = 2
        CHOICES = ((IOS, 'iOS'), (ANDROID, 'Android'))

    class Meta:
        unique_together = 'user', 'device_id', 'type'

    type = models.SmallIntegerField(choices=Type.CHOICES)
    device_id = models.CharField(max_length=255, null=True, unique=True)
    device_manufacturer = models.CharField(max_length=255, null=True, blank=True)
    device_model = models.CharField(max_length=255, null=True, blank=True)
    token = models.CharField(max_length=255, null=True, unique=True)

    def process(self, event):
        """ Send events as push notification via Google Cloud Messaging.
            Expected settings as follows:

                # https://developers.google.com/mobile/add
                WALDUR_CORE['GOOGLE_API'] = {
                    'NOTIFICATION_TITLE': "Waldur notification",
                    'Android': {
                        'server_key': 'AIzaSyA2_7UaVIxXfKeFvxTjQNZbrzkXG9OTCkg',
                    },
                    'iOS': {
                        'server_key': 'AIzaSyA34zlG_y5uHOe2FmcJKwfk2vG-3RW05vk',
                    }
                }
        """

        conf = settings.WALDUR_CORE.get('GOOGLE_API') or {}
        keys = conf.get(dict(self.Type.CHOICES)[self.type])

        if not keys or not self.token:
            return

        endpoint = 'https://gcm-http.googleapis.com/gcm/send'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'key=%s' % keys['server_key'],
        }
        payload = {
            'to': self.token,
            'notification': {
                'body': event.get('message', 'New event'),
                'title': conf.get('NOTIFICATION_TITLE', 'Waldur notification'),
                'image': 'icon',
            },
            'data': {
                'event': event
            },
        }
        if self.type == self.Type.IOS:
            payload['content-available'] = '1'
        logger.debug('Submitting GCM push notification with headers %s, payload: %s' % (headers, payload))
        requests.post(endpoint, json=payload, headers=headers)


class EmailHook(BaseHook):
    email = models.EmailField(max_length=75)

    def process(self, event):
        if not self.email:
            logger.debug('Skipping processing of email hook (PK=%s) because email is not defined' % self.pk)
            return
        # Prevent mutations of event because otherwise subsequent hook processors would fail
        context = event.copy()
        subject = settings.WALDUR_CORE.get('NOTIFICATION_SUBJECT', 'Notifications from Waldur')
        context['timestamp'] = timestamp_to_datetime(event['timestamp'])
        text_message = context['message']
        html_message = render_to_string('logging/email.html', {'events': [context]})
        logger.debug('Submitting email hook to %s, payload: %s', self.email, context)
        send_mail(subject, text_message, settings.DEFAULT_FROM_EMAIL, [self.email], html_message=html_message)


class SystemNotification(EventTypesMixin, models.Model):
    # Model doesn't inherit NameMixin, because this is circular dependence.
    name = models.CharField(_('name'), max_length=150)
    hook_content_type = models.ForeignKey(ct_models.ContentType, related_name='+')
    roles = JSONField('List of roles', default=list)

    @staticmethod
    def get_valid_roles():
        return 'admin', 'manager', 'owner'

    @classmethod
    def get_hooks(cls, event_type, project=None, customer=None):
        from waldur_core.structure import models as structure_models
        from waldur_core.logging import loggers

        groups = [g[0] for g in loggers.event_logger.get_all_groups().items() if event_type in g[1]]

        for hook in cls.objects.filter(models.Q(event_types__contains=event_type) |
                                       models.Q(event_groups__has_any_keys=groups)):
            hook_class = hook.hook_content_type.model_class()
            users_qs = []

            if project:
                if 'admin' in hook.roles:
                    users_qs.append(project.get_users(structure_models.ProjectRole.ADMINISTRATOR))
                if 'manager' in hook.roles:
                    users_qs.append(project.get_users(structure_models.ProjectRole.MANAGER))
                if 'owner' in hook.roles:
                    users_qs.append(project.customer.get_owners())

            if customer:
                if 'owner' in hook.roles:
                    users_qs.append(customer.get_owners())

            if len(users_qs) > 1:
                users = users_qs[0].union(*users_qs[1:]).distinct()
            elif len(users_qs) == 1:
                users = users_qs[0]
            else:
                users = []

            for user in users:
                if user.email:
                    yield hook_class(
                        user=user,
                        event_types=hook.event_types,
                        email=user.email
                    )

    def __str__(self):
        return '%s | %s' % (self.hook_content_type, self.name)


class Report(UuidMixin, TimeStampedModel):
    class States(object):
        PENDING = 'pending'
        DONE = 'done'
        ERRED = 'erred'

        CHOICES = (
            (PENDING, 'Pending'),
            (DONE, 'Done'),
            (ERRED, 'Erred'),
        )

    file = models.FileField(upload_to='logging_reports')
    file_size = models.PositiveIntegerField(null=True)
    state = models.CharField(choices=States.CHOICES, default=States.PENDING, max_length=10)
    error_message = models.TextField(blank=True)
