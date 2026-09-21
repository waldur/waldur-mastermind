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


def log_offering_merge_event(
    merge: models.OfferingMerge,
    event_type: EventType,
    message: str,
    level="info",
    **context,
):
    """Emit an offering merge event, scoped to the target and every source.

    The context names the sources and the target and carries the non-zero
    counts of the stored preview, so the event log shows what the merge moved.
    ``message`` may use ``{merge_uuid}``, ``{target_name}`` and
    ``{source_names}``.
    """
    sources = list(merge.sources.order_by("id"))
    counts = {
        label: count
        for label, count in ((merge.preview or {}).get("counts") or {}).items()
        if count
    }
    event_logger.emit(
        message,
        event_type=event_type,
        event_context={
            "merge_uuid": merge.uuid.hex,
            "merge_state": merge.state,
            "target_uuid": merge.target.uuid.hex,
            "target_name": merge.target.name,
            "source_uuids": [source.uuid.hex for source in sources],
            "source_names": ", ".join(source.name for source in sources),
            "counts": counts,
            **context,
        },
        scopes=[merge.target, *sources],
        level=level,
    )


def log_offering_merge_created(merge: models.OfferingMerge):
    log_offering_merge_event(
        merge,
        EventType.MARKETPLACE_OFFERING_MERGE_CREATED,
        "Merge {merge_uuid} of offerings {source_names} into {target_name} "
        "has been created.",
    )


def log_offering_merge_executed(merge: models.OfferingMerge):
    log_offering_merge_event(
        merge,
        EventType.MARKETPLACE_OFFERING_MERGE_EXECUTED,
        "Offerings {source_names} have been merged into {target_name}.",
    )


def log_offering_merge_failed(merge: models.OfferingMerge, operation: str):
    log_offering_merge_event(
        merge,
        EventType.MARKETPLACE_OFFERING_MERGE_FAILED,
        "The {operation} of merge {merge_uuid} of offerings {source_names} into "
        "{target_name} has failed: {error_message}",
        level="warning",
        operation=operation,
        error_message=merge.error_message,
    )


def log_offering_merge_undone(merge: models.OfferingMerge):
    log_offering_merge_event(
        merge,
        EventType.MARKETPLACE_OFFERING_MERGE_UNDONE,
        "Merge {merge_uuid} of offerings {source_names} into {target_name} "
        "has been undone.",
    )


def log_offering_merge_verification_failed(merge: models.OfferingMerge):
    verification = merge.verification or {}
    stage = verification.get("stage", "")
    report = verification.get(stage) or {}
    failed = [
        check["code"] for check in report.get("checks", []) if not check["passed"]
    ]
    log_offering_merge_event(
        merge,
        EventType.MARKETPLACE_OFFERING_MERGE_VERIFICATION_FAILED,
        "Verification after the {stage} of merge {merge_uuid} into "
        "{target_name} has failed: {failed_checks}.",
        level="warning",
        stage=stage,
        failed_checks=", ".join(failed),
    )
