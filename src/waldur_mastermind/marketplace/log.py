from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_mastermind.marketplace import models


def get_resource_scopes(resource: models.Resource):
    return [resource, resource.project, resource.project.customer]


def get_order_scopes(order: models.Order):
    return [order, order.project, order.project.customer, order.resource]


def get_maintenance_announcement_scopes(
    maintenance: models.MaintenanceAnnouncement,
):
    return [maintenance, maintenance.service_provider.customer]


def log_resource_limit_update_succeeded(resource: models.Resource):
    event_logger.emit(
        "Limits of resource {resource_name} have been updated.",
        event_type=EventType.MARKETPLACE_RESOURCE_UPDATE_LIMITS_SUCCEEDED,
        event_context={"resource": resource},
        scopes=get_resource_scopes(resource),
    )


def log_resource_end_date_has_been_updated(resource, user, template=None):
    template = template or (
        "End date of marketplace resource %(resource_name)s has been updated."
        " End date: %(end_date)s."
        " User: %(user)s."
    )

    context = {
        "resource_name": resource.name,
        "end_date": resource.end_date,
        "user": user,
    }

    event_logger.emit(
        template % context,
        event_type=EventType.MARKETPLACE_RESOURCE_UPDATE_END_DATE_SUCCEEDED,
        event_context={
            "resource": resource,
        },
        scopes=get_resource_scopes(resource),
    )
