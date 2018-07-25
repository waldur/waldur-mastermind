""" Formatters, handlers and other stuff for default logging configuration """

import datetime
import json
import logging

from celery import current_app


class EventFormatter(logging.Formatter):

    def format_timestamp(self, time):
        return datetime.datetime.utcfromtimestamp(time).isoformat() + 'Z'

    def levelname_to_importance(self, levelname):
        if levelname == 'DEBUG':
            return 'low'
        elif levelname == 'INFO':
            return 'normal'
        elif levelname == 'WARNING':
            return 'high'
        elif levelname == 'ERROR':
            return 'very high'
        else:
            return 'critical'

    def format(self, record):
        message = {
            # basic
            '@timestamp': self.format_timestamp(record.created),
            '@version': 1,
            'message': record.getMessage(),

            # logging details
            'levelname': record.levelname,
            'logger': record.name,
            'importance': self.levelname_to_importance(record.levelname),
            'importance_code': record.levelno,
        }

        if hasattr(record, 'event_type'):
            message['event_type'] = record.event_type

        if hasattr(record, 'event_context'):
            message.update(record.event_context)

        return json.dumps(message)


class EventLoggerAdapter(logging.LoggerAdapter, object):
    """ LoggerAdapter """

    def __init__(self, logger):
        super(EventLoggerAdapter, self).__init__(logger, {})

    def process(self, msg, kwargs):
        if 'extra' in kwargs:
            kwargs['extra']['event'] = True
        else:
            kwargs['extra'] = {'event': True}
        return msg, kwargs


class RequireEvent(logging.Filter):
    """ A filter that allows only event records. """

    def filter(self, record):
        return getattr(record, 'event', False)


class RequireNotEvent(logging.Filter):
    """ A filter that allows only non-event records. """

    def filter(self, record):
        return not getattr(record, 'event', False)


class RequireNotBackgroundTask(logging.Filter):
    """ Filter out messages from background tasks """

    def filter(self, record):
        try:
            name = getattr(record, 'data', {})['name']
            task = current_app.tasks[name]
        except KeyError:
            return True
        is_background = getattr(task, 'is_background', False)
        return not is_background


class TCPEventHandler(logging.handlers.SocketHandler, object):

    def __init__(self, host='localhost', port=5959):
        super(TCPEventHandler, self).__init__(host, int(port))
        self.formatter = EventFormatter()

    def makePickle(self, record):
        return self.formatter.format(record) + b'\n'


class HookHandler(logging.Handler):
    def emit(self, record):
        # Check that record contains event
        if hasattr(record, 'event_type') and hasattr(record, 'event_context'):

            # Convert record to plain dictionary
            event = {
                'timestamp': record.created,
                'levelname': record.levelname,
                'message': record.getMessage(),
                'type': record.event_type,
                'context': record.event_context
            }
            # XXX: This import provides circular dependencies between core and
            #      logging applications.
            from waldur_core.core.tasks import send_task
            # Perform hook processing in background thread
            send_task('logging', 'process_event')(event)
