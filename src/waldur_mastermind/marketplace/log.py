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


def log_resource_plan_switched(
    resource: models.Resource,
    old_plan: models.Plan,
    new_plan: models.Plan,
    old_billing: str,
    new_billing: str,
):
    if old_billing == new_billing:
        message = (
            "Plan of resource {resource_name} has been switched "
            f"from {old_plan.name} to {new_plan.name} ({new_billing} billing)."
        )
    else:
        message = (
            "Plan of resource {resource_name} has been switched "
            f"from {old_plan.name} ({old_billing} billing) "
            f"to {new_plan.name} ({new_billing} billing)."
        )
    event_logger.emit(
        message,
        event_type=EventType.MARKETPLACE_RESOURCE_PLAN_SWITCHED,
        event_context={
            "resource": resource,
            "old_plan_name": old_plan.name,
            "new_plan_name": new_plan.name,
            "old_plan_billing": old_billing,
            "new_plan_billing": new_billing,
        },
        scopes=get_resource_scopes(resource),
    )


def log_resource_limit_update_succeeded(resource: models.Resource):
    event_logger.emit(
        "Limits of resource {resource_name} have been updated.",
        event_type=EventType.MARKETPLACE_RESOURCE_UPDATE_LIMITS_SUCCEEDED,
        event_context={"resource": resource},
        scopes=get_resource_scopes(resource),
    )


def log_resource_api_key_rotated(api_key: models.ResourceApiKey, user):
    # A resource owns many keys — the audit event must identify which one.
    resource = api_key.resource
    event_logger.emit(
        f"API key {api_key.client_id or api_key.uuid.hex} of resource "
        f"{resource.name} has been rotated by {user}.",
        event_type=EventType.MARKETPLACE_RESOURCE_API_KEY_ROTATED,
        event_context={"resource": resource},
        scopes=get_resource_scopes(resource),
    )


def log_resource_api_key_revealed(api_key: models.ResourceApiKey, user):
    resource = api_key.resource
    event_logger.emit(
        f"API key {api_key.client_id or api_key.uuid.hex} of resource "
        f"{resource.name} has been revealed to {user}.",
        event_type=EventType.MARKETPLACE_RESOURCE_API_KEY_REVEALED,
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


def get_resource_project_scopes(resource_project: models.ResourceProject):
    resource = resource_project.resource
    return [
        resource_project,
        resource,
        resource.project,
        resource.project.customer,
    ]


def log_resource_project_created(resource_project: models.ResourceProject):
    event_logger.emit(
        "Resource project {resource_project_name} has been created "
        "in resource {resource_name}.",
        event_type=EventType.MARKETPLACE_RESOURCE_PROJECT_CREATED,
        event_context={
            "resource_project": resource_project,
            "resource": resource_project.resource,
        },
        scopes=get_resource_project_scopes(resource_project),
    )


def log_resource_project_removed(resource_project: models.ResourceProject):
    event_logger.emit(
        "Resource project {resource_project_name} has been removed "
        "from resource {resource_name}.",
        event_type=EventType.MARKETPLACE_RESOURCE_PROJECT_REMOVED,
        event_context={
            "resource_project": resource_project,
            "resource": resource_project.resource,
        },
        scopes=get_resource_project_scopes(resource_project),
    )


def log_resource_project_recovered(resource_project: models.ResourceProject):
    event_logger.emit(
        "Resource project {resource_project_name} has been recovered "
        "in resource {resource_name}.",
        event_type=EventType.MARKETPLACE_RESOURCE_PROJECT_RECOVERED,
        event_context={
            "resource_project": resource_project,
            "resource": resource_project.resource,
        },
        scopes=get_resource_project_scopes(resource_project),
    )
