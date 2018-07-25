import six

from django.contrib.auth import get_user_model

from waldur_core.logging.loggers import EventLogger, event_logger


User = get_user_model()

provider_event_type_mapping = {
    'google': 'auth_logged_in_with_google',
    'facebook': 'auth_logged_in_with_facebook',
    'smartid.ee': 'auth_logged_in_with_smartid_ee',
}


class SocialEventLogger(EventLogger):
    provider = six.text_type
    user = User

    class Meta:
        event_types = provider_event_type_mapping.values()
        event_groups = {'users': event_types}


event_logger.register('auth_social', SocialEventLogger)
