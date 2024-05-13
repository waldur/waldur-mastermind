from waldur_core.logging.loggers import EventLogger, event_logger
from waldur_core.structure import models


class CustomerEventLogger(EventLogger):
    customer = models.Customer

    class Meta:
        event_types = (
            "customer_deletion_succeeded",
            "customer_update_succeeded",
            "customer_creation_succeeded",
        )
        event_groups = {
            "customers": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {event_context["customer"]}


class ProjectEventLogger(EventLogger):
    project = models.Project

    class Meta:
        event_types = (
            "project_deletion_triggered",
            "project_deletion_succeeded",
            "project_update_succeeded",
            "project_creation_succeeded",
        )
        event_groups = {
            "projects": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        project = event_context["project"]
        return {project, project.customer}


class ResourceEventLogger(EventLogger):
    resource = models.BaseResource

    class Meta:
        event_types = (
            "resource_start_scheduled",
            "resource_start_succeeded",
            "resource_start_failed",
            "resource_stop_scheduled",
            "resource_stop_succeeded",
            "resource_stop_failed",
            "resource_restart_scheduled",
            "resource_restart_succeeded",
            "resource_restart_failed",
            "resource_creation_scheduled",
            "resource_creation_succeeded",
            "resource_creation_failed",
            "resource_import_succeeded",
            "resource_update_succeeded",
            "resource_deletion_scheduled",
            "resource_deletion_succeeded",
            "resource_deletion_failed",
        )
        event_groups = {
            "resources": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        resource = event_context["resource"]
        return {resource, resource.project, resource.project.customer}


class AccessSubnetEventLogger(EventLogger):
    access_subnet = models.AccessSubnet

    class Meta:
        event_types = (
            "access_subnet_creation_succeeded",
            "access_subnet_update_succeeded",
            "access_subnet_deletion_succeeded",
        )
        event_groups = {
            "access_subnets": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        access_subnet = event_context["access_subnet"]
        return {access_subnet, access_subnet.customer}


event_logger.register("customer", CustomerEventLogger)
event_logger.register("project", ProjectEventLogger)
event_logger.register("resource", ResourceEventLogger)
event_logger.register("access_subnet", AccessSubnetEventLogger)
