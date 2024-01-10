from waldur_core.core.models import User
from waldur_core.logging.loggers import EventLogger, event_logger


class CustomerRoleEventLogger(EventLogger):
    customer = "structure.Customer"
    affected_user = User
    user = User
    structure_type = str
    role_name = str

    class Meta:
        event_types = "role_granted", "role_revoked", "role_updated"
        event_groups = {
            "customers": event_types,
            "users": event_types,
        }
        nullable_fields = ["user"]

    @staticmethod
    def get_scopes(event_context):
        return {event_context["customer"]}


class ProjectRoleEventLogger(EventLogger):
    project = "structure.Project"
    user = User
    affected_user = User
    structure_type = str
    role_name = str

    class Meta:
        event_types = "role_granted", "role_revoked", "role_updated"
        event_groups = {
            "projects": event_types,
            "users": event_types,
        }
        nullable_fields = ["user"]

    @staticmethod
    def get_scopes(event_context):
        project = event_context["project"]
        return {project, project.customer}


class OfferingRoleEventLogger(EventLogger):
    offering = "marketplace.Offering"
    user = User
    affected_user = User
    structure_type = str
    role_name = str

    class Meta:
        event_types = "role_granted", "role_revoked", "role_updated"
        event_groups = {
            "customers": event_types,
            "users": event_types,
        }
        nullable_fields = ["user"]

    @staticmethod
    def get_scopes(event_context):
        return {event_context["offering"].customer}


event_logger.register("customer_role", CustomerRoleEventLogger)
event_logger.register("project_role", ProjectRoleEventLogger)
event_logger.register("offering_role", OfferingRoleEventLogger)
