"""Event loggers that allows to store logs in DB"""

import logging
from decimal import Decimal

from constance import config

from waldur_core.logging import models
from waldur_core.logging.enums import (
    EVENT_GROUP_MAPPING,
    RESOURCE_CHANGE_EVENTS,
    EventGroup,
    EventType,
)
from waldur_core.logging.log import EventLoggerAdapter
from waldur_core.logging.middleware import get_event_context
from waldur_core.logging.mixins import LoggableMixin

logger = logging.getLogger(__name__)

event_logger = EventLoggerAdapter(logger)


def compile_context(**kwargs):
    context = {}

    event_context = get_event_context()
    if event_context:
        context.update(event_context)

    for entity_name, entity in kwargs.items():
        if isinstance(entity, LoggableMixin):
            context.update(entity._get_log_context(entity_name))
        elif isinstance(entity, int | float | str | dict | tuple | list | bool):
            context[entity_name] = entity
        elif isinstance(entity, Decimal):
            context[entity_name] = str(entity)
        elif entity is None:
            pass
        else:
            context[entity_name] = str(entity)
            logger.warning(
                "Cannot properly serialize '%s' context field. "
                "Must be inherited from LoggableMixin." % entity_name
            )

    return context


def emit(
    message_template: str,
    event_type: EventType,
    event_context: dict | None = None,
    scopes=None,
    level="info",
):
    if not config.NOTIFY_ABOUT_RESOURCE_CHANGE and event_type in RESOURCE_CHANGE_EVENTS:
        return

    if not event_context:
        event_context = {}

    context = compile_context(**event_context)
    msg = str(message_template).format(**context)
    log = getattr(event_logger, level)
    log(msg, extra={"event_type": event_type, "event_context": context})

    event = models.Event.objects.create(
        event_type=event_type,
        message=msg,
        context=context,
    )
    if event_context:
        for scope in scopes or []:
            if scope and scope.id:
                models.Feed.objects.create(scope=scope, event=event)


def get_valid_events():
    return [item.value for item in EventType]


def get_event_groups():
    return {
        key: [item.value for item in value]
        for key, value in EVENT_GROUP_MAPPING.items()
    }


def get_event_groups_keys():
    return [item.value for item in EventGroup]


def expand_event_groups(groups):
    items = set()
    for group in groups:
        items.update([item.value for item in EVENT_GROUP_MAPPING.get(group, [])])
    return sorted(items)
