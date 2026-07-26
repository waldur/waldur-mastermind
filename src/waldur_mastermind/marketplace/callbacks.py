import logging

from constance import config
from django.core import exceptions as django_exceptions
from django.db import transaction
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from waldur_core.core.enums import CoreStates
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes, ResourceStates

from . import log, models, signals, tasks
from .utils import format_limits_list

logger = logging.getLogger(__name__)


def create_resource_plan_period(resource: models.Resource):
    models.ResourcePlanPeriod.objects.create(
        resource=resource,
        plan=resource.plan,
        start=now(),
        end=None,
    )


def close_resource_plan_period(resource: models.Resource):
    models.ResourcePlanPeriod.objects.filter(
        resource=resource,
        plan=resource.plan,
        end=None,
    ).update(end=now())


def resource_creation_succeeded(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        OrderTypes.CREATE,
        OrderStates.DONE,
        validate,
    )

    if resource.state != ResourceStates.OK:
        resource.set_state_ok()
        resource.save(update_fields=["state"])

    signals.resource_creation_succeeded.send(sender=models.Resource, instance=resource)
    event_logger.emit(
        "Resource {resource_name} has been created.",
        event_type=EventType.MARKETPLACE_RESOURCE_CREATE_SUCCEEDED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
    )
    return order


def resource_creation_failed(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        OrderTypes.CREATE,
        OrderStates.ERRED,
        validate,
    )
    resource.set_state_erred()
    resource.save(update_fields=["state"])

    if order:
        copy_error_from_resource_to_order(resource, order)

    event_logger.emit(
        "Resource {resource_name} creation has failed.",
        event_type=EventType.MARKETPLACE_RESOURCE_CREATE_FAILED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
        level="error",
    )
    return order


def copy_error_from_resource_to_order(resource, order):
    update_fields = set()
    for field in ("error_message", "error_traceback"):
        new_value = getattr(resource.scope, field, "")
        current_value = getattr(order, field, "")
        if new_value != current_value:
            setattr(order, field, new_value)
            update_fields.add(field)
    if update_fields:
        order.save(update_fields=update_fields)


def resource_creation_canceled(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        OrderTypes.CREATE,
        OrderStates.CANCELED,
        validate,
    )

    if resource.state != ResourceStates.TERMINATED:
        resource.set_state_terminated()
        resource.save(update_fields=["state"])

    event_logger.emit(
        "Resource {resource_name} creation has been canceled.",
        event_type=EventType.MARKETPLACE_RESOURCE_CREATE_CANCELED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
    )
    return order


def resource_restore_succeeded(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        OrderTypes.RESTORE,
        OrderStates.DONE,
        validate,
    )

    if resource.state != ResourceStates.OK:
        resource.set_state_ok()
        resource.save(update_fields=["state"])

    signals.resource_creation_succeeded.send(sender=models.Resource, instance=resource)
    event_logger.emit(
        "Resource {resource_name} has been restored.",
        event_type=EventType.MARKETPLACE_RESOURCE_CREATE_SUCCEEDED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
    )
    return order


def resource_restore_failed(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        OrderTypes.RESTORE,
        OrderStates.ERRED,
        validate,
    )
    resource.set_state_erred()
    resource.save(update_fields=["state"])

    if order:
        copy_error_from_resource_to_order(resource, order)

    event_logger.emit(
        "Resource {resource_name} restoration has failed.",
        event_type=EventType.MARKETPLACE_RESOURCE_CREATE_FAILED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
        level="error",
    )
    return order


def resource_restore_canceled(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        OrderTypes.RESTORE,
        OrderStates.CANCELED,
        validate,
    )

    if resource.state != ResourceStates.TERMINATED:
        resource.set_state_terminated()
        resource.save(update_fields=["state"])

    event_logger.emit(
        "Resource {resource_name} restoration has been canceled.",
        event_type=EventType.MARKETPLACE_RESOURCE_CREATE_CANCELED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
    )
    return order


def resource_update_succeeded(resource: models.Resource, validate=False):
    """
    Handle successful resource update completion.

    This function is called either:
    1. From process_order flow (via _process_options_update, etc.)
    2. From set_state_done API endpoint (via sync_order_state)

    Uses select_for_update() to prevent race conditions when multiple
    processes try to update the resource simultaneously.
    """
    with transaction.atomic():
        # Lock the resource row to prevent concurrent modifications
        locked_resource = models.Resource.objects.select_for_update().get(
            pk=resource.pk
        )

        order = set_order_state(
            locked_resource,
            OrderTypes.UPDATE,
            OrderStates.DONE,
            validate,
        )

        email_context = {
            "resource_name": locked_resource.name,
            "support_email": config.SITE_EMAIL,
            "support_phone": config.SITE_PHONE,
        }

        if locked_resource.state != ResourceStates.OK:
            locked_resource.set_state_ok()

        limits_changed = False
        if order:
            email_context.update(
                {
                    "order_user": order.created_by.get_full_name(),
                }
            )

            plan_changed = bool(order.plan and locked_resource.plan != order.plan)
            limits_changed = bool(
                order.limits and locked_resource.limits != order.limits
            )

            # Handle options updates from order attributes
            new_options = order.attributes.get("new_options")
            if new_options:
                current_options = locked_resource.options or {}
                current_options.update(new_options)
                locked_resource.options = current_options
                logger.info(
                    "Updated options for resource %s (UUID: %s) from order %s",
                    locked_resource.name,
                    locked_resource.uuid.hex,
                    order.uuid.hex,
                )

            if plan_changed:
                email_context.update(
                    {
                        "resource_old_plan": locked_resource.plan.name,
                        "resource_plan": order.plan.name,
                    }
                )
                locked_resource.plan = order.plan
                transaction.on_commit(
                    lambda: tasks.notify_about_resource_change.delay(
                        "marketplace_resource_update_succeeded",
                        email_context,
                        locked_resource.uuid,
                    )
                )
            if limits_changed:
                components_map = order.offering.get_limit_components()
                email_context.update(
                    {
                        "resource_old_limits": format_limits_list(
                            components_map, locked_resource.limits
                        ),
                        "resource_limits": format_limits_list(
                            components_map, order.limits
                        ),
                    }
                )
                locked_resource.limits = order.limits
                transaction.on_commit(
                    lambda: tasks.notify_about_resource_change.delay(
                        "marketplace_resource_update_limits_succeeded",
                        email_context,
                        locked_resource.uuid,
                    )
                )

            if plan_changed or limits_changed:
                locked_resource.init_cost()

        locked_resource.save()

        if limits_changed:
            log.log_resource_limit_update_succeeded(locked_resource)

        if limits_changed and (locked_resource.downscaled or locked_resource.paused):
            resource_uuid = str(locked_resource.uuid)
            offering_id = locked_resource.offering_id
            transaction.on_commit(
                lambda: _trigger_slurm_policy_reevaluation(resource_uuid, offering_id)
            )

        return order


def _trigger_slurm_policy_reevaluation(resource_uuid, offering_id):
    """Trigger immediate SLURM policy re-evaluation after limit changes.

    When resource limits increase on a downscaled/paused resource,
    the policy system needs to re-evaluate usage percentages so that
    QoS restrictions are lifted promptly instead of waiting for the
    next periodic evaluation cycle.
    """
    from waldur_mastermind.policy import models as policy_models
    from waldur_mastermind.policy import tasks as policy_tasks

    policies = policy_models.SlurmPeriodicUsagePolicy.objects.filter(
        scope_id=offering_id,
    )
    for policy in policies:
        policy_tasks.evaluate_resource_against_policy.delay(
            resource_uuid, str(policy.uuid)
        )


def resource_update_failed(resource: models.Resource, validate=False):
    """
    Handle failed resource update.

    Uses select_for_update() to prevent race conditions when multiple
    processes try to update the resource simultaneously.
    """
    with transaction.atomic():
        # Lock the resource row to prevent concurrent modifications
        locked_resource = models.Resource.objects.select_for_update().get(
            pk=resource.pk
        )

        order = set_order_state(
            locked_resource,
            OrderTypes.UPDATE,
            OrderStates.ERRED,
            validate,
        )
        if locked_resource.state != ResourceStates.ERRED:
            locked_resource.set_state_erred()
            locked_resource.save(update_fields=["state"])
        else:
            logger.info(
                "Resource %s is already in erred state, skip transition",
                locked_resource,
            )

        event_logger.emit(
            "Resource {resource_name} update has failed.",
            event_type=EventType.MARKETPLACE_RESOURCE_UPDATE_FAILED,
            event_context={"resource": locked_resource},
            scopes=log.get_resource_scopes(locked_resource),
            level="error",
        )
        return order


def resource_update_canceled(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        OrderTypes.UPDATE,
        OrderStates.CANCELED,
        validate,
    )

    if resource.state != ResourceStates.OK:
        resource.set_state_ok()
        resource.save(update_fields=["state"])
    else:
        logger.info("Resource %s is already in OK state, skip transition", resource)

    event_logger.emit(
        "Resource {resource_name} update has canceled.",
        event_type=EventType.MARKETPLACE_RESOURCE_UPDATE_CANCELED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
        level="error",
    )
    return order


def resource_deletion_succeeded(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        OrderTypes.TERMINATE,
        OrderStates.DONE,
        validate,
    )
    if resource.state != ResourceStates.TERMINATED:
        resource.set_state_terminated()
        resource.save(update_fields=["state"])
    else:
        logger.info(
            "Resource %s is already in terminated state, skip transition", resource
        )

    # Terminated resources keep their row, so the ResourceApiKey FK cascade never
    # fires. Delete the key rows explicitly (the agent's delete_resource already
    # removed the gateway Secret entries) so no orphan OK keys remain revealable.
    deleted, _ = resource.api_keys.all().delete()
    if deleted:
        logger.info(
            "Deleted %s API key row(s) of terminated resource %s", deleted, resource
        )

    signals.resource_deletion_succeeded.send(models.Resource, instance=resource)
    event_logger.emit(
        "Resource {resource_name} has been deleted.",
        event_type=EventType.MARKETPLACE_RESOURCE_TERMINATE_SUCCEEDED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
    )
    return order


def resource_deletion_failed(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        OrderTypes.TERMINATE,
        OrderStates.ERRED,
        validate,
    )
    if resource.state != ResourceStates.OK:
        resource.set_state_ok()
        resource.save(update_fields=["state"])
    else:
        logger.info("Resource %s is already in OK state, skip transition", resource)

    event_logger.emit(
        "Resource {resource_name} deletion has failed.",
        event_type=EventType.MARKETPLACE_RESOURCE_TERMINATE_FAILED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
        level="error",
    )
    return order


def resource_deletion_canceled(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        OrderTypes.TERMINATE,
        OrderStates.CANCELED,
        validate,
    )

    if resource.state != ResourceStates.OK:
        resource.set_state_ok()
        resource.save(update_fields=["state"])

    event_logger.emit(
        "Resource {resource_name} terminate has canceled.",
        event_type=EventType.MARKETPLACE_RESOURCE_TERMINATE_CANCELED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
        level="error",
    )
    return order


def resource_erred_on_backend(resource: models.Resource, validate=False):
    if resource.state == ResourceStates.ERRED:
        return

    resource.set_state_erred()
    resource.save(update_fields=["state"])

    event_logger.emit(
        "Resource {resource_name} got error on backend.",
        event_type=EventType.MARKETPLACE_RESOURCE_ERRED_ON_BACKEND,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
    )


def set_order_state(resource: models.Resource, order_type, new_state, validate=False):
    try:
        order = models.Order.objects.get(
            resource=resource,
            type=order_type,
            state=OrderStates.EXECUTING,
        )
    except django_exceptions.ObjectDoesNotExist:
        if validate:
            raise ValidationError(
                _("Unable to complete action because related order is not found.")
            )
        logger.debug(
            "Skipping order synchronization for marketplace resource "
            "because order is not found. Resource ID: %s",
            resource.id,
        )
    except django_exceptions.MultipleObjectsReturned:
        if validate:
            raise ValidationError(
                _(
                    "Unable to complete action because multiple related orders are found."
                )
            )
        logger.debug(
            "Skipping order synchronization for marketplace resource "
            "because there are multiple active orders are found. "
            "Resource ID: %s",
            resource.id,
        )
    else:
        getattr(order, OrderStateRouter[new_state])()
        order.save(update_fields=["state"])
        return order


StateRouter = {
    (CoreStates.CREATING, CoreStates.OK): resource_creation_succeeded,
    (CoreStates.CREATING, CoreStates.ERRED): resource_creation_failed,
    (CoreStates.CREATION_SCHEDULED, CoreStates.ERRED): resource_creation_failed,
    (CoreStates.UPDATING, CoreStates.OK): resource_update_succeeded,
    (CoreStates.UPDATING, CoreStates.ERRED): resource_update_failed,
    (CoreStates.UPDATE_SCHEDULED, CoreStates.ERRED): resource_update_failed,
    (CoreStates.DELETING, CoreStates.ERRED): resource_deletion_failed,
    (CoreStates.DELETION_SCHEDULED, CoreStates.ERRED): resource_deletion_failed,
    (CoreStates.OK, CoreStates.ERRED): resource_erred_on_backend,
}


OrderStateRouter = {
    OrderStates.EXECUTING: "set_state_executing",
    OrderStates.DONE: "complete",
    OrderStates.ERRED: "set_state_erred",
    OrderStates.CANCELED: "cancel",
}


OrderHandlers = {
    (
        OrderTypes.CREATE,
        OrderStates.DONE,
    ): resource_creation_succeeded,
    (
        OrderTypes.CREATE,
        OrderStates.ERRED,
    ): resource_creation_failed,
    (
        OrderTypes.CREATE,
        OrderStates.CANCELED,
    ): resource_creation_canceled,
    (
        OrderTypes.UPDATE,
        OrderStates.DONE,
    ): resource_update_succeeded,
    (
        OrderTypes.UPDATE,
        OrderStates.ERRED,
    ): resource_update_failed,
    (
        OrderTypes.UPDATE,
        OrderStates.CANCELED,
    ): resource_update_canceled,
    (
        OrderTypes.TERMINATE,
        OrderStates.DONE,
    ): resource_deletion_succeeded,
    (
        OrderTypes.TERMINATE,
        OrderStates.ERRED,
    ): resource_deletion_failed,
    (
        OrderTypes.TERMINATE,
        OrderStates.CANCELED,
    ): resource_deletion_canceled,
    (
        OrderTypes.RESTORE,
        OrderStates.DONE,
    ): resource_restore_succeeded,
    (
        OrderTypes.RESTORE,
        OrderStates.ERRED,
    ): resource_restore_failed,
    (
        OrderTypes.RESTORE,
        OrderStates.CANCELED,
    ): resource_restore_canceled,
}


def sync_resource_state(instance, resource):
    key = (instance.tracker.previous("state"), instance.state)
    func = StateRouter.get(key)
    if func:
        func(resource)


def sync_order_state(order, new_state):
    key = (order.type, new_state)
    func = OrderHandlers.get(key)
    if func:
        func(order.resource)
