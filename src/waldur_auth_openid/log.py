from django.contrib.auth import get_user_model

from waldur_core.logging.loggers import EventLogger, event_logger


User = get_user_model()


class OpenIDEventLogger(EventLogger):
    user = User

    class Meta:
        event_types = ('auth_logged_in_with_openid',)
        event_groups = {'users': event_types}


event_logger.register('auth_openid', OpenIDEventLogger)
