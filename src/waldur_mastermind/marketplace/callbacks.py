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
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates

from . import log, models, signals, tasks, utils

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
        models.RequestTypeMixin.Types.CREATE,
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
        models.RequestTypeMixin.Types.CREATE,
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
        models.RequestTypeMixin.Types.CREATE,
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


def resource_update_succeeded(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.UPDATE,
        OrderStates.DONE,
        validate,
    )

    email_context = {
        "resource_name": resource.name,
        "support_email": config.SITE_EMAIL,
        "support_phone": config.SITE_PHONE,
    }

    if resource.state != ResourceStates.OK:
        resource.set_state_ok()
        resource.save(update_fields=["state"])

    if order:
        email_context.update(
            {
                "order_user": order.created_by.get_full_name(),
            }
        )

        plan_changed = bool(order.plan and resource.plan != order.plan)
        limits_changed = bool(order.limits and resource.limits != order.limits)
        if plan_changed:
            email_context.update(
                {
                    "resource_old_plan": resource.plan.name,
                    "resource_plan": order.plan.name,
                }
            )
            resource.plan = order.plan
            transaction.on_commit(
                lambda: tasks.notify_about_resource_change.delay(
                    "marketplace_resource_update_succeeded",
                    email_context,
                    resource.uuid,
                )
            )
        if limits_changed:
            components_map = order.offering.get_limit_components()
            email_context.update(
                {
                    "resource_old_limits": utils.format_limits_list(
                        components_map, resource.limits
                    ),
                    "resource_limits": utils.format_limits_list(
                        components_map, order.limits
                    ),
                }
            )
            resource.limits = order.limits
            transaction.on_commit(
                lambda: tasks.notify_about_resource_change.delay(
                    "marketplace_resource_update_limits_succeeded",
                    email_context,
                    resource.uuid,
                )
            )

        if plan_changed or limits_changed:
            resource.init_cost()
            resource.save()
            if limits_changed:
                log.log_resource_limit_update_succeeded(resource)

    return order


def resource_update_failed(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.UPDATE,
        OrderStates.ERRED,
        validate,
    )
    if resource.state != ResourceStates.ERRED:
        resource.set_state_erred()
        resource.save(update_fields=["state"])
    else:
        logger.info("Resource %s is already in erred state, skip transition", resource)

    event_logger.emit(
        "Resource {resource_name} update has failed.",
        event_type=EventType.MARKETPLACE_RESOURCE_UPDATE_FAILED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
        level="error",
    )
    return order


def resource_update_canceled(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.UPDATE,
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
        models.RequestTypeMixin.Types.TERMINATE,
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
        models.RequestTypeMixin.Types.TERMINATE,
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
        models.RequestTypeMixin.Types.TERMINATE,
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
        models.Order.Types.CREATE,
        OrderStates.DONE,
    ): resource_creation_succeeded,
    (
        models.Order.Types.CREATE,
        OrderStates.ERRED,
    ): resource_creation_failed,
    (
        models.Order.Types.CREATE,
        OrderStates.CANCELED,
    ): resource_creation_canceled,
    (
        models.Order.Types.UPDATE,
        OrderStates.DONE,
    ): resource_update_succeeded,
    (
        models.Order.Types.UPDATE,
        OrderStates.ERRED,
    ): resource_update_failed,
    (
        models.Order.Types.UPDATE,
        OrderStates.CANCELED,
    ): resource_update_canceled,
    (
        models.Order.Types.TERMINATE,
        OrderStates.DONE,
    ): resource_deletion_succeeded,
    (
        models.Order.Types.TERMINATE,
        OrderStates.ERRED,
    ): resource_deletion_failed,
    (
        models.Order.Types.TERMINATE,
        OrderStates.CANCELED,
    ): resource_deletion_canceled,
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
