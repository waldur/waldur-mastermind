import six

from waldur_core.core.models import User, SshPublicKey
from waldur_core.logging.loggers import EventLogger, event_logger


class AuthEventLogger(EventLogger):
    user = User
    username = six.text_type

    class Meta:
        event_types = ('auth_logged_in_with_username',
                       'auth_login_failed_with_username',
                       'auth_logged_out')
        event_groups = {'users': event_types}
        nullable_fields = ['user', 'username']


class UserEventLogger(EventLogger):
    affected_user = User

    class Meta:
        permitted_objects_uuids = staticmethod(lambda user: {'user_uuid': [user.uuid.hex]})
        event_types = ('user_creation_succeeded',
                       'user_update_succeeded',
                       'user_deletion_succeeded',
                       'user_password_updated',
                       'user_token_lifetime_updated',
                       'user_activated',
                       'user_deactivated',
                       'user_profile_changed')
        event_groups = {
            'users': event_types,
        }


class TokenEventLogger(EventLogger):
    affected_user = User

    class Meta:
        event_types = ('token_created',)


class SshPublicKeyEventLogger(EventLogger):
    ssh_key = SshPublicKey
    user = User

    class Meta:
        event_types = ('ssh_key_creation_succeeded',
                       'ssh_key_deletion_succeeded')
        event_groups = {
            'ssh': event_types,
            'users': event_types,
        }


event_logger.register('auth', AuthEventLogger)
event_logger.register('user', UserEventLogger)
event_logger.register('sshkey', SshPublicKeyEventLogger)
event_logger.register('token', TokenEventLogger)
