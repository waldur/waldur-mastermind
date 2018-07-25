""" Custom loggers that allows to store logs in DB and Elastic """

from __future__ import unicode_literals

from collections import defaultdict
import datetime
import decimal
import importlib
import logging
import types
import uuid

from django.apps import apps
from django.contrib.contenttypes import models as ct_models
from django.db import transaction, IntegrityError
import six

from waldur_core.logging import models
from waldur_core.logging.log import EventLoggerAdapter
from waldur_core.logging.middleware import get_event_context

logger = logging.getLogger(__name__)


class LoggerError(AttributeError):
    pass


class EventLoggerError(AttributeError):
    pass


class AlertLoggerError(AttributeError):
    pass


class BaseLogger(object):
    def __init__(self, logger_name=__name__):
        self._meta = getattr(self, 'Meta', None)
        self.supported_types = self.get_supported_types()

    def get_supported_types(self):
        raise NotImplementedError

    def get_supported_groups(self):
        raise NotImplementedError

    def get_nullable_fields(self):
        return getattr(self._meta, 'nullable_fields', [])

    def get_field_model(self, model):
        if not isinstance(model, six.string_types):
            return model

        try:
            return apps.get_model(model)
        except LookupError:
            try:
                app_name, class_name = model.split('.')
                module = importlib.import_module('waldur_core.%s.models' % app_name)
                return getattr(module, class_name)
            except (ImportError, AttributeError, IndexError):
                raise LoggerError("Can't find model %s" % model)

    def compile_message(self, message_template, context):
        try:
            msg = six.text_type(message_template).format(**context)
        except KeyError as e:
            raise LoggerError(
                "Cannot find %s context field. Choices are: %s" % (
                    six.text_type(e), ', '.join(context.keys())))
        return msg

    def validate_logging_type(self, logging_type):
        if self.supported_types and logging_type not in self.supported_types:
            raise EventLoggerError(
                "Unsupported logging type '%s'. Choices are: %s" % (
                    logging_type, ', '.join(self.supported_types)))

    def compile_context(self, **kwargs):
        # Get a list of fields here in order to be sure all models already loaded.
        if not hasattr(self, 'fields'):
            self.fields = {
                k: self.get_field_model(v)
                for k, v in self.__class__.__dict__.items()
                if not k.startswith('_') and not isinstance(v, types.FunctionType) and k != 'Meta'}

        missed = set(self.fields.keys()) - set(self.get_nullable_fields()) - set(kwargs.keys())
        if missed:
            raise LoggerError("Missed fields in event context: %s" % ', '.join(missed))

        context = {}

        event_context = get_event_context()
        if event_context:
            context.update(event_context)
            username = event_context.get('user_username')
            if 'user' in self.fields and username:
                logger.warning("User is passed directly to event context. "
                               "Currently authenticated user %s is ignored.", username)

        for entity_name, entity in six.iteritems(kwargs):
            if entity_name in self.fields:
                entity_class = self.fields[entity_name]
                if entity is None and entity_name in self.get_nullable_fields():
                    continue
                if not isinstance(entity, entity_class):
                    raise LoggerError(
                        "Field '%s' must be an instance of %s but %s received" % (
                            entity_name, entity_class.__name__, entity.__class__.__name__))
            else:
                logger.error(
                    "Field '%s' cannot be used in logging context for %s",
                    entity_name, self.__class__.__name__)
                continue

            if isinstance(entity, LoggableMixin):
                context.update(entity._get_log_context(entity_name))
            elif isinstance(entity, (int, float, six.string_types, dict, tuple, list, bool)):
                context[entity_name] = entity
            elif entity is None:
                pass
            else:
                context[entity_name] = six.text_type(entity)
                logger.warning(
                    "Cannot properly serialize '%s' context field. "
                    "Must be inherited from LoggableMixin." % entity_name)

        return context


class EventLogger(BaseLogger):
    """ Base event logger API.
        Fields which must be passed during event log emitting (event context)
        should be defined as attributes for this class in the form of:

        field_name = ObjectClass || '<app_label>.<class_name>'

        A list of supported event types can be defined with help of method get_supported_types,
        or 'event_types' property of Meta class. Event type won't be validated if this list is empty.

        Example usage:

        .. code-block:: python

            from waldur_core.openstack.models import Tenant

            class QuotaEventLogger(EventLogger):
                tenant = Tenant
                project = 'structure.Project'
                threshold = float
                quota_type = six.string_types

                class Meta:
                    event_types = 'quota_threshold_reached',


            quota_logger = QuotaEventLogger(__name__)
            quota_logger.warning(
                '{quota_type} quota threshold has been reached for {project_name}.',
                event_type='quota_threshold_reached',
                event_context=dict(
                    quota_type=quota.name,
                    project=membership.project,
                    tenant=tenant,
                    threshold=threshold * quota.limit)
            )
    """

    def __init__(self, logger_name=__name__):
        super(EventLogger, self).__init__(logger_name)
        self.logger = EventLoggerAdapter(logging.getLogger(logger_name))

    def get_supported_types(self):
        return getattr(self._meta, 'event_types', tuple())

    def get_supported_groups(self):
        return getattr(self._meta, 'event_groups', {})

    def info(self, *args, **kwargs):
        self.process('info', *args, **kwargs)

    def error(self, *args, **kwargs):
        self.process('error', *args, **kwargs)

    def warning(self, *args, **kwargs):
        self.process('warning', *args, **kwargs)

    def debug(self, *args, **kwargs):
        self.process('debug', *args, **kwargs)

    def process(self, level, message_template, event_type='undefined', event_context=None):
        self.validate_logging_type(event_type)

        if not event_context:
            event_context = {}

        context = self.compile_context(**event_context)
        msg = self.compile_message(message_template, context)

        log = getattr(self.logger, level)
        log(msg, extra={'event_type': event_type, 'event_context': context})


class AlertLogger(BaseLogger):
    """ Base alert logger API.

        Fields which must be passed during alert log emitting (alert context)
        should be defined as attributes for this class in the form of:

        field_name = ObjectClass || '<app_label>.<class_name>'

        A list of supported event types can be defined with help of method get_supported_types,
        or 'alert_types' property of Meta class. Event type won't be validated if this list is empty.

        Example usage:

        .. code-block:: python

            from waldur_core.logging.loggers import AlertLogger, alert_logger
            from waldur_core.quotas import models

            class QuotaAlertLogger(AlertLogger):
                quota = models.Quota

                class Meta:
                    alert_types = ('quota_usage_is_over_threshold', )

            alert_logger.register('quota', QuotaAlertLogger)


            alert_logger.quota.warning(
                'Quota {quota_name} is over threshold. Limit: {quota_limit}, usage: {quota_usage}',
                scope=quota,
                alert_type='quota_usage_is_over_threshold',
                alert_context={
                    'quota': quota
                })
    """

    def get_supported_types(self):
        return getattr(self._meta, 'alert_types', tuple())

    def get_supported_groups(self):
        return getattr(self._meta, 'alert_groups', {})

    def info(self, *args, **kwargs):
        return self.process(models.Alert.SeverityChoices.INFO, *args, **kwargs)

    def error(self, *args, **kwargs):
        return self.process(models.Alert.SeverityChoices.ERROR, *args, **kwargs)

    def warning(self, *args, **kwargs):
        return self.process(models.Alert.SeverityChoices.WARNING, *args, **kwargs)

    def debug(self, *args, **kwargs):
        return self.process(models.Alert.SeverityChoices.DEBUG, *args, **kwargs)

    def process(self, severity, message_template, scope, alert_type='undefined', alert_context=None, fail_silently=True):
        self.validate_logging_type(alert_type)

        if not alert_context:
            alert_context = {}

        context = self.compile_context(**alert_context)
        msg = self.compile_message(message_template, context)
        content_type = ct_models.ContentType.objects.get_for_model(scope)

        try:
            with transaction.atomic():
                alert = models.Alert.objects.select_for_update().get(
                    content_type=content_type,
                    object_id=scope.id,
                    alert_type=alert_type,
                    closed__isnull=True
                )
                if alert.severity != severity or alert.message != msg:
                    alert.severity = severity
                    alert.message = msg
                    alert.save()

                    logger.info(
                        'Updated alert for scope %s (id: %s), with type %s',
                        scope, scope.id, alert_type)

                return alert, False
        except models.Alert.DoesNotExist:
            pass

        try:
            alert = models.Alert.objects.create(
                scope=scope,
                alert_type=alert_type,
                severity=severity,
                message=msg,
                context=context
            )
            logger.info(
                'Created new alert for scope %s (id: %s), with type %s',
                scope, scope.id, alert_type)
            return alert, True
        except IntegrityError:
            logger.warning(
                'Could not create alert for scope %s (id: %s), with type %s due to concurrent update',
                scope, scope.id, alert_type)
            if fail_silently:
                return None, False
            else:
                raise

    def close(self, scope, alert_type):
        try:
            content_type = ct_models.ContentType.objects.get_for_model(scope)
            alert = models.Alert.objects.get(
                object_id=scope.id, content_type=content_type, alert_type=alert_type, closed__isnull=True)
            alert.close()
        except models.Alert.DoesNotExist:
            pass


class LoggableMixin(object):
    """ Mixin to serialize model in logs.
        Extends django model or custom class with fields extraction method.
    """

    def get_log_fields(self):
        return ('uuid', 'name')

    def filter_by_logged_object(self):
        """
        Return query dictionary to search current object in ElasticSearch.
        Model may return custom query, but backend doesn't need to know details.
        """
        return {
            self.__class__.__name__ + '_uuid': self.uuid.hex
        }

    def _get_log_context(self, entity_name):

        context = {}
        for field in self.get_log_fields():
            if not hasattr(self, field):
                continue

            value = getattr(self, field)

            name = "{}_{}".format(entity_name, field)
            if isinstance(value, uuid.UUID):
                context[name] = value.hex
            elif isinstance(value, LoggableMixin):
                context.update(value._get_log_context(field))
            elif isinstance(value, datetime.date):
                context[name] = value.isoformat()
            elif isinstance(value, decimal.Decimal):
                context[name] = float(value)
            elif isinstance(value, dict):
                context[name] = value
            else:
                context[name] = six.text_type(value)

        return context

    @classmethod
    def get_permitted_objects_uuids(cls, user):
        return {}


class BaseLoggerRegistry(object):

    def get_loggers(self):
        raise NotImplementedError('Method "get_loggers" is not implemented.')

    def register(self, name, logger_class):
        if name in self.__dict__:
            raise EventLoggerError("Logger '%s' already registered." % name)
        self.__dict__[name] = logger_class()

    def unregister_all(self):
        self.__dict__ = {}

    def get_all_types(self):
        events = set()
        for elogger in self.get_loggers():
            events.update(elogger.get_supported_types())
        return list(sorted(events))

    def get_all_groups(self):
        groups = defaultdict(set)
        for elogger in self.get_loggers():
            for group, items in elogger.get_supported_groups().items():
                groups[group].update(set(items))
        return dict(groups)

    def expand_groups(self, groups):
        items = set()
        mapping = self.get_all_groups()
        for group in groups:
            items.update(mapping.get(group, []))
        return sorted(items)


class EventLoggerRegistry(BaseLoggerRegistry):

    def get_loggers(self):
        return [l for l in self.__dict__.values() if isinstance(l, EventLogger)]

    def get_permitted_objects_uuids(self, user):
        from waldur_core.logging.utils import get_loggable_models
        permitted_objects_uuids = {}
        for model in get_loggable_models():
            for field, uuids in model.get_permitted_objects_uuids(user).items():
                permitted_objects_uuids[field] = [uuid_obj.hex for uuid_obj in uuids]
        return permitted_objects_uuids


class AlertLoggerRegistry(BaseLoggerRegistry):

    def get_loggers(self):
        return [l for l in self.__dict__.values() if isinstance(l, AlertLogger)]


def get_valid_events():
    return event_logger.get_all_types()


def get_valid_alerts():
    return alert_logger.get_all_types()


def get_event_groups():
    return event_logger.get_all_groups()


def get_event_groups_keys():
    return sorted(get_event_groups().keys())


def get_alert_groups():
    return alert_logger.get_all_groups()


def expand_event_groups(groups):
    return event_logger.expand_groups(groups)


def expand_alert_groups(groups):
    return alert_logger.expand_groups(groups)


class CustomEventLogger(EventLogger):
    """
    Logger for custom events e.g. created by staff user
    """
    scope = LoggableMixin

    class Meta:
        event_types = ('custom_notification',)
        nullable_fields = ('scope',)


class ThresholdAlertLogger(AlertLogger):
    object = 'logging.AlertThresholdMixin'

    class Meta:
        alert_types = ('threshold_exceeded',)


# This global objects represent the default loggers registry
event_logger = EventLoggerRegistry()
alert_logger = AlertLoggerRegistry()

event_logger.register('custom', CustomEventLogger)
alert_logger.register('threshold', ThresholdAlertLogger)
