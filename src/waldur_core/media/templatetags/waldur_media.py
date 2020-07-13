from django import template
from django.conf import settings

from waldur_core.logging.middleware import get_event_context
from waldur_core.media.utils import encode_protected_url, s3_to_waldur_media_url

register = template.Library()


@register.simple_tag()
def protected_url(value, field):
    context = get_event_context()
    if settings.USE_PROTECTED_URL:
        url = value.url
        if (
            settings.CONVERT_MEDIA_URLS_TO_MASTERMIND_NETLOC
        ):  # If using s3-compatible storage
            url = s3_to_waldur_media_url(url, context['request'])
        return url
    return encode_protected_url(value.instance, field, user_uuid=context['user_uuid'])
