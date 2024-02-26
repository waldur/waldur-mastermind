from waldur_core.core.models import User
from waldur_core.logging.loggers import EventLogger, event_logger


class UserRoleEventLogger(EventLogger):
    scope = object
    scope_type = str
    scope_uuid = str
    scope_name = str
    customer = "structure.Customer"
    affected_user = User
    user = User
    role_name = str

    class Meta:
        event_types = "role_granted", "role_revoked", "role_updated"
        event_groups = {
            "permissions": event_types,
        }
        nullable_fields = ["user"]

    @staticmethod
    def get_scopes(event_context):
        scope = event_context["scope"]
        customer = event_context["customer"]
        return {scope, customer}


event_logger.register("user_role", UserRoleEventLogger)
