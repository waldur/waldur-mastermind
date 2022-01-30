import logging

from django.conf import settings
from django.core import exceptions as django_exceptions
from django.db import transaction
from django.utils.timezone import now
from django.utils.translation import ugettext_lazy as _
from rest_framework.exceptions import ValidationError

from waldur_core.core.models import StateMixin

from . import log, models, signals, tasks, utils

logger = logging.getLogger(__name__)


def create_resource_plan_period(resource: models.Resource):
    models.ResourcePlanPeriod.objects.create(
        resource=resource, plan=resource.plan, start=now(), end=None,
    )


@transaction.atomic()
def close_resource_plan_period(resource: models.Resource):
    try:
        previous_period = models.ResourcePlanPeriod.objects.select_for_update().get(
            resource=resource, plan=resource.plan, end=None,
        )
        previous_period.end = now()
        previous_period.save(update_fields=['end'])
    except django_exceptions.ObjectDoesNotExist:
        logger.warning(
            'Skipping previous resource plan period update '
            'because it does not exist. Resource ID: %s, plan ID: %s.',
            resource.id,
            resource.plan.id,
        )
    except django_exceptions.MultipleObjectsReturned:
        logger.warning(
            'Skipping previous resource plan period update '
            'because multiple objects found. Resource ID: %s, plan ID: %s.',
            resource.id,
            resource.plan.id,
        )


def resource_creation_succeeded(resource: models.Resource, validate=False):
    order_item = set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.CREATE,
        models.OrderItem.States.DONE,
        validate,
    )

    if resource.state != resource.States.OK:
        resource.set_state_ok()
        resource.save(update_fields=['state'])

    if resource.plan:
        create_resource_plan_period(resource)

    signals.resource_creation_succeeded.send(sender=models.Resource, instance=resource)
    log.log_resource_creation_succeeded(resource)
    return order_item


def resource_creation_failed(resource: models.Resource, validate=False):
    order_item = set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.CREATE,
        models.OrderItem.States.ERRED,
        validate,
    )
    resource.set_state_erred()
    resource.save(update_fields=['state'])

    log.log_resource_creation_failed(resource)
    return order_item


def resource_creation_canceled(resource: models.Resource, validate=False):
    order_item = set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.CREATE,
        models.OrderItem.States.TERMINATED,
        validate,
    )

    if resource.state != resource.States.TERMINATED:
        resource.set_state_terminated()
        resource.save(update_fields=['state'])

    log.log_resource_creation_canceled(resource)
    return order_item


def resource_update_succeeded(resource: models.Resource, validate=False):
    order_item = set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.UPDATE,
        models.OrderItem.States.DONE,
        validate,
    )

    email_context = {
        'resource_name': resource.name,
        'support_email': settings.WALDUR_CORE['SITE_EMAIL'],
        'support_phone': settings.WALDUR_CORE['SITE_PHONE'],
        'order_item_user': order_item.order.created_by.get_full_name(),
    }

    if resource.state != models.Resource.States.OK:
        resource.set_state_ok()
        resource.save(update_fields=['state'])
    if order_item and order_item.plan:
        if resource.plan != order_item.plan:
            email_context.update(
                {
                    'resource_old_plan': resource.plan.name,
                    'resource_plan': order_item.plan.name,
                }
            )

        close_resource_plan_period(resource)

        resource.plan = order_item.plan
        resource.init_cost()
        resource.save(update_fields=['plan', 'cost'])

        create_resource_plan_period(resource)
        transaction.on_commit(
            lambda: tasks.notify_about_resource_change.delay(
                'marketplace_resource_update_succeeded', email_context, resource.uuid
            )
        )
    if order_item and order_item.limits:
        components_map = order_item.offering.get_limit_components()
        email_context.update(
            {
                'resource_old_limits': utils.format_limits_list(
                    components_map, resource.limits
                ),
                'resource_limits': utils.format_limits_list(
                    components_map, order_item.limits
                ),
            }
        )
        resource.limits = order_item.limits
        resource.init_cost()
        resource.save(update_fields=['limits', 'cost'])
        log.log_resource_limit_update_succeeded(resource)
        transaction.on_commit(
            lambda: tasks.notify_about_resource_change.delay(
                'marketplace_resource_update_limits_succeeded',
                email_context,
                resource.uuid,
            )
        )

    log.log_resource_update_succeeded(resource)
    return order_item


def resource_update_failed(resource: models.Resource, validate=False):
    order_item = set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.UPDATE,
        models.OrderItem.States.ERRED,
        validate,
    )
    resource.set_state_erred()
    resource.save(update_fields=['state'])

    log.log_resource_update_failed(resource)
    return order_item


def resource_deletion_succeeded(resource: models.Resource, validate=False):
    order_item = set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.TERMINATE,
        models.OrderItem.States.DONE,
        validate,
    )
    resource.set_state_terminated()
    resource.save(update_fields=['state'])

    if resource.plan:
        close_resource_plan_period(resource)

    signals.resource_deletion_succeeded.send(models.Resource, instance=resource)
    log.log_resource_terminate_succeeded(resource)
    return order_item


def resource_deletion_failed(resource: models.Resource, validate=False):
    order_item = set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.TERMINATE,
        models.OrderItem.States.ERRED,
        validate,
    )
    resource.set_state_ok()
    resource.save(update_fields=['state'])

    log.log_resource_terminate_failed(resource)
    return order_item


def set_order_item_state(
    resource: models.Resource, order_item_type, new_state, validate=False
):
    try:
        order_item = models.OrderItem.objects.get(
            resource=resource,
            type=order_item_type,
            state=models.OrderItem.States.EXECUTING,
        )
    except django_exceptions.ObjectDoesNotExist:
        if validate:
            raise ValidationError(
                _('Unable to complete action because related order item is not found.')
            )
        logger.debug(
            'Skipping order item synchronization for marketplace resource '
            'because order item is not found. Resource ID: %s',
            resource.id,
        )
    except django_exceptions.MultipleObjectsReturned:
        if validate:
            raise ValidationError(
                _(
                    'Unable to complete action because multiple related order items are found.'
                )
            )
        logger.debug(
            'Skipping order item synchronization for marketplace resource '
            'because there are multiple active order items are found. '
            'Resource ID: %s',
            resource.id,
        )
    else:
        getattr(order_item, OrderItemStateRouter[new_state])()
        order_item.save(update_fields=['state'])
        return order_item


States = StateMixin.States


StateRouter = {
    (States.CREATING, States.OK): resource_creation_succeeded,
    (States.CREATING, States.ERRED): resource_creation_failed,
    (States.UPDATING, States.OK): resource_update_succeeded,
    (States.UPDATING, States.ERRED): resource_update_failed,
    (States.DELETING, States.ERRED): resource_deletion_failed,
}


OrderItemStateRouter = {
    models.OrderItem.States.EXECUTING: 'set_state_executing',
    models.OrderItem.States.DONE: 'set_state_done',
    models.OrderItem.States.ERRED: 'set_state_erred',
    models.OrderItem.States.TERMINATED: 'set_state_terminated',
    models.OrderItem.States.TERMINATING: 'set_state_terminating',
}


OrderItemHandlers = {
    (
        models.OrderItem.Types.CREATE,
        models.OrderItem.States.DONE,
    ): resource_creation_succeeded,
    (
        models.OrderItem.Types.CREATE,
        models.OrderItem.States.ERRED,
    ): resource_creation_failed,
    (
        models.OrderItem.Types.CREATE,
        models.OrderItem.States.TERMINATED,
    ): resource_creation_canceled,
    (
        models.OrderItem.Types.UPDATE,
        models.OrderItem.States.DONE,
    ): resource_update_succeeded,
    (
        models.OrderItem.Types.UPDATE,
        models.OrderItem.States.ERRED,
    ): resource_update_failed,
    (
        models.OrderItem.Types.UPDATE,
        models.OrderItem.States.TERMINATED,
    ): resource_update_failed,
    (
        models.OrderItem.Types.TERMINATE,
        models.OrderItem.States.DONE,
    ): resource_deletion_succeeded,
    (
        models.OrderItem.Types.TERMINATE,
        models.OrderItem.States.ERRED,
    ): resource_deletion_failed,
    (
        models.OrderItem.Types.TERMINATE,
        models.OrderItem.States.TERMINATED,
    ): resource_deletion_failed,
}


def sync_resource_state(instance, resource):
    key = (instance.tracker.previous('state'), instance.state)
    func = StateRouter.get(key)
    if func:
        func(resource)


def sync_order_item_state(order_item, new_state):
    key = (order_item.type, new_state)
    func = OrderItemHandlers.get(key)
    if func:
        func(order_item.resource)
