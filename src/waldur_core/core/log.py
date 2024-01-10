from rest_framework.request import Request

from waldur_core.core.models import SshPublicKey, User
from waldur_core.logging.loggers import EventLogger, event_logger

from . import utils


class AuthEventMixin:
    @staticmethod
    def request_context_processor(context, request):
        # This value will be set later if waldur geo module is enable.
        context["location"] = "pending"

        # ip_address will be get from threading.local(). The core middleware will set it.
        context["user_agent"] = utils.get_user_agent(request)
        context.update(utils.get_device_info(context["user_agent"]))

    @property
    def fields(self):
        if not hasattr(self, "_fields"):
            self._fields = super().fields
            self._fields["request"] = Request
        return self._fields

    def get_nullable_fields(self):
        return super().get_nullable_fields() + ["request"]


class AuthEventLogger(AuthEventMixin, EventLogger):
    user = User
    username = str

    class Meta:
        event_types = (
            "auth_logged_in_with_username",
            "auth_login_failed_with_username",
            "auth_logged_out",
        )
        event_groups = {"auth": event_types}
        nullable_fields = ["user", "username"]

    @staticmethod
    def get_scopes(event_context):
        if "user" in event_context:
            return {event_context["user"]}
        else:
            return set()


class UserEventLogger(EventLogger):
    affected_user = User

    class Meta:
        permitted_objects_uuids = staticmethod(
            lambda user: {"user_uuid": [user.uuid.hex]}
        )
        event_types = (
            "user_creation_succeeded",
            "user_update_succeeded",
            "user_details_update_succeeded",
            "user_deletion_succeeded",
            "user_password_updated",
            "user_activated",
            "user_deactivated",
            "user_profile_changed",
        )
        event_groups = {
            "users": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {event_context["affected_user"]}


class TokenEventLogger(EventLogger):
    affected_user = User

    class Meta:
        event_types = (
            "token_created",
            "token_lifetime_updated",
        )
        event_groups = {
            "auth": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {event_context["affected_user"]}


class SshPublicKeyEventLogger(EventLogger):
    ssh_key = SshPublicKey
    user = User

    class Meta:
        event_types = ("ssh_key_creation_succeeded", "ssh_key_deletion_succeeded")
        event_groups = {
            "ssh": event_types,
            "users": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {event_context["user"]}


event_logger.register("auth", AuthEventLogger)
event_logger.register("user", UserEventLogger)
event_logger.register("sshkey", SshPublicKeyEventLogger)
event_logger.register("token", TokenEventLogger)
