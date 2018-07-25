import six
from waldur_core.core.models import User
from waldur_core.logging.loggers import EventLogger, event_logger


class FreeIPAEventLogger(EventLogger):
    user = User
    username = six.text_type

    class Meta:
        event_types = (
            'freeipa_profile_created',
            'freeipa_profile_deleted',
            'freeipa_profile_enabled',
            'freeipa_profile_disabled',
        )
        event_groups = {'users': event_types}


event_logger.register('freeipa', FreeIPAEventLogger)
