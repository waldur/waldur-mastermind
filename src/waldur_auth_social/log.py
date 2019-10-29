from django.contrib.auth import get_user_model

from waldur_core.logging.loggers import EventLogger, event_logger


User = get_user_model()

provider_event_type_mapping = {
    'google': 'auth_logged_in_with_google',
    'facebook': 'auth_logged_in_with_facebook',
    'smartid.ee': 'auth_logged_in_with_smartid_ee',
    'tara': 'auth_logged_in_with_tara',
}


class SocialEventLogger(EventLogger):
    provider = str
    user = User

    class Meta:
        event_types = provider_event_type_mapping.values()
        event_groups = {'users': event_types}

    @staticmethod
    def get_scopes(event_context):
        return {event_context['user']}


event_logger.register('auth_social', SocialEventLogger)
