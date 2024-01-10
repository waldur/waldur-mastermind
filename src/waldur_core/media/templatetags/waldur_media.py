from django import template
from django.conf import settings

from waldur_core.logging.middleware import get_event_context
from waldur_core.media.utils import encode_protected_url

register = template.Library()


@register.simple_tag()
def protected_url(value, field):
    context = get_event_context()
    if not settings.USE_PROTECTED_URL:
        return value.url
    return encode_protected_url(value.instance, field, user_uuid=context["user_uuid"])
