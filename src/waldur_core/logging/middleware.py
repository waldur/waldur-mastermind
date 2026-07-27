import threading

from django.utils.deprecation import MiddlewareMixin

from waldur_core.core import utils as core_utils
from waldur_core.core.auth_utils import AUTH_METHOD_PAT, get_auth_method

_locals = threading.local()


def get_event_context():
    return getattr(_locals, "context", None)


def set_event_context(context):
    _locals.context = context


def reset_event_context():
    if hasattr(_locals, "context"):
        del _locals.context


def set_current_user(user):
    context = get_event_context() or {}
    context.update(user._get_log_context("user"))
    set_event_context(context)


def set_current_auth(auth):
    """Record how the request was authenticated on the event context.

    Every event emitted during the request inherits this, so a resource action
    performed with a token is distinguishable from the same action performed in
    a browser session without touching any call site.
    """
    context = get_event_context() or {}
    auth_method = get_auth_method(auth)
    context["auth_method"] = auth_method
    if auth_method == AUTH_METHOD_PAT:
        context["pat_uuid"] = auth.uuid.hex
        context["pat_name"] = auth.name
    set_event_context(context)


class CaptureEventContextMiddleware(MiddlewareMixin):
    def process_request(self, request):
        context = {}

        # Requests without a resolvable IP (management commands, some test
        # clients) must still contribute user context — an early return here
        # used to drop it entirely.
        ip_address = core_utils.get_ip_address(request)
        if ip_address:
            context["ip_address"] = ip_address

        user_agent = request.META.get("HTTP_USER_AGENT")
        if user_agent:
            context["user_agent"] = user_agent

        user = getattr(request, "user", None)
        if user and not user.is_anonymous:
            context.update(user._get_log_context("user"))

        set_event_context(context)

    def process_response(self, request, response):
        reset_event_context()
        return response
