from __future__ import unicode_literals

import threading

from django.utils.deprecation import MiddlewareMixin

_locals = threading.local()


def get_event_context():
    return getattr(_locals, 'context', None)


def set_event_context(context):
    _locals.context = context


def reset_event_context():
    if hasattr(_locals, 'context'):
        del _locals.context


def set_current_user(user):
    context = get_event_context() or {}
    context.update(user._get_log_context('user'))
    set_event_context(context)


def get_ip_address(request):
    """
    Correct IP address is expected as first element of HTTP_X_FORWARDED_FOR or REMOTE_ADDR
    """
    if 'HTTP_X_FORWARDED_FOR' in request.META:
        return request.META['HTTP_X_FORWARDED_FOR'].split(',')[0].strip()
    else:
        return request.META['REMOTE_ADDR']


class CaptureEventContextMiddleware(MiddlewareMixin):
    def process_request(self, request):
        context = {'ip_address': get_ip_address(request)}

        user = getattr(request, 'user', None)
        if user and not user.is_anonymous:
            context.update(user._get_log_context('user'))

        set_event_context(context)

    def process_response(self, request, response):
        reset_event_context()
        return response
