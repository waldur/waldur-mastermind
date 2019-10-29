from waldur_core.core.models import User
from waldur_core.logging.loggers import EventLogger, event_logger


class FreeIPAEventLogger(EventLogger):
    user = User
    username = str

    class Meta:
        event_types = (
            'freeipa_profile_created',
            'freeipa_profile_deleted',
            'freeipa_profile_enabled',
            'freeipa_profile_disabled',
        )
        event_groups = {'users': event_types}

    @staticmethod
    def get_scopes(event_context):
        return {event_context['user']}


event_logger.register('freeipa', FreeIPAEventLogger)
