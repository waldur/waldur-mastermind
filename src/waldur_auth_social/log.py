from django.contrib.auth import get_user_model

from waldur_core.core.log import AuthEventMixin
from waldur_core.logging.loggers import EventLogger, event_logger

User = get_user_model()


class SocialEventLogger(AuthEventMixin, EventLogger):
    provider = str
    user = User

    class Meta:
        event_types = ['auth_logged_in_with_oauth']
        event_groups = {'users': event_types}

    @staticmethod
    def get_scopes(event_context):
        return {event_context['user']}


event_logger.register('auth_social', SocialEventLogger)
