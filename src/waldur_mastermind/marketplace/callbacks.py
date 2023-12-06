import logging

from constance import config
from django.core import exceptions as django_exceptions
from django.db import transaction
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from waldur_core.core.models import StateMixin

from . import log, models, signals, tasks, utils

logger = logging.getLogger(__name__)


def create_resource_plan_period(resource: models.Resource):
    models.ResourcePlanPeriod.objects.create(
        resource=resource,
        plan=resource.plan,
        start=now(),
        end=None,
    )


@transaction.atomic()
def close_resource_plan_period(resource: models.Resource):
    try:
        previous_period = models.ResourcePlanPeriod.objects.select_for_update().get(
            resource=resource,
            plan=resource.plan,
            end=None,
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
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.CREATE,
        models.Order.States.DONE,
        validate,
    )

    if resource.state != resource.States.OK:
        resource.set_state_ok()
        resource.save(update_fields=['state'])

    if resource.plan:
        create_resource_plan_period(resource)

    signals.resource_creation_succeeded.send(sender=models.Resource, instance=resource)
    log.log_resource_creation_succeeded(resource)
    return order


def resource_creation_failed(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.CREATE,
        models.Order.States.ERRED,
        validate,
    )
    resource.set_state_erred()
    resource.save(update_fields=['state'])

    log.log_resource_creation_failed(resource)
    return order


def resource_creation_canceled(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.CREATE,
        models.Order.States.CANCELED,
        validate,
    )

    if resource.state != resource.States.TERMINATED:
        resource.set_state_terminated()
        resource.save(update_fields=['state'])

    log.log_resource_creation_canceled(resource)
    return order


def resource_update_succeeded(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.UPDATE,
        models.Order.States.DONE,
        validate,
    )

    email_context = {
        'resource_name': resource.name,
        'support_email': config.SITE_EMAIL,
        'support_phone': config.SITE_PHONE,
    }

    if resource.state != models.Resource.States.OK:
        resource.set_state_ok()
        resource.save(update_fields=['state'])

    if order:
        email_context.update(
            {
                'order_user': order.created_by.get_full_name(),
            }
        )

    if order and order.plan:
        if resource.plan != order.plan:
            email_context.update(
                {
                    'resource_old_plan': resource.plan.name,
                    'resource_plan': order.plan.name,
                }
            )

        close_resource_plan_period(resource)

        resource.plan = order.plan
        resource.init_cost()
        resource.save(update_fields=['plan', 'cost'])

        create_resource_plan_period(resource)
        transaction.on_commit(
            lambda: tasks.notify_about_resource_change.delay(
                'marketplace_resource_update_succeeded', email_context, resource.uuid
            )
        )
    if order and order.limits:
        components_map = order.offering.get_limit_components()
        email_context.update(
            {
                'resource_old_limits': utils.format_limits_list(
                    components_map, resource.limits
                ),
                'resource_limits': utils.format_limits_list(
                    components_map, order.limits
                ),
            }
        )
        resource.limits = order.limits
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
    return order


def resource_update_failed(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.UPDATE,
        models.Order.States.ERRED,
        validate,
    )
    resource.set_state_erred()
    resource.save(update_fields=['state'])

    log.log_resource_update_failed(resource)
    return order


def resource_update_canceled(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.UPDATE,
        models.Order.States.CANCELED,
        validate,
    )

    if resource.state != resource.States.OK:
        resource.set_state_ok()
        resource.save(update_fields=['state'])

    log.log_resource_update_canceled(resource)
    return order


def resource_deletion_succeeded(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.TERMINATE,
        models.Order.States.DONE,
        validate,
    )
    resource.set_state_terminated()
    resource.save(update_fields=['state'])

    if resource.plan:
        close_resource_plan_period(resource)

    signals.resource_deletion_succeeded.send(models.Resource, instance=resource)
    log.log_resource_terminate_succeeded(resource)
    return order


def resource_deletion_failed(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.TERMINATE,
        models.Order.States.ERRED,
        validate,
    )
    resource.set_state_ok()
    resource.save(update_fields=['state'])

    log.log_resource_terminate_failed(resource)
    return order


def resource_deletion_canceled(resource: models.Resource, validate=False):
    order = set_order_state(
        resource,
        models.RequestTypeMixin.Types.TERMINATE,
        models.Order.States.CANCELED,
        validate,
    )

    if resource.state != resource.States.OK:
        resource.set_state_ok()
        resource.save(update_fields=['state'])

    log.log_resource_terminate_canceled(resource)
    return order


def set_order_state(resource: models.Resource, order_type, new_state, validate=False):
    try:
        order = models.Order.objects.get(
            resource=resource,
            type=order_type,
            state=models.Order.States.EXECUTING,
        )
    except django_exceptions.ObjectDoesNotExist:
        if validate:
            raise ValidationError(
                _('Unable to complete action because related order is not found.')
            )
        logger.debug(
            'Skipping order synchronization for marketplace resource '
            'because order is not found. Resource ID: %s',
            resource.id,
        )
    except django_exceptions.MultipleObjectsReturned:
        if validate:
            raise ValidationError(
                _(
                    'Unable to complete action because multiple related orders are found.'
                )
            )
        logger.debug(
            'Skipping order synchronization for marketplace resource '
            'because there are multiple active orders are found. '
            'Resource ID: %s',
            resource.id,
        )
    else:
        getattr(order, OrderStateRouter[new_state])()
        order.save(update_fields=['state'])
        return order


States = StateMixin.States


StateRouter = {
    (States.CREATING, States.OK): resource_creation_succeeded,
    (States.CREATING, States.ERRED): resource_creation_failed,
    (States.UPDATING, States.OK): resource_update_succeeded,
    (States.UPDATING, States.ERRED): resource_update_failed,
    (States.DELETING, States.ERRED): resource_deletion_failed,
}


OrderStateRouter = {
    models.Order.States.EXECUTING: 'set_state_executing',
    models.Order.States.DONE: 'complete',
    models.Order.States.ERRED: 'set_state_erred',
    models.Order.States.CANCELED: 'cancel',
}


OrderHandlers = {
    (
        models.Order.Types.CREATE,
        models.Order.States.DONE,
    ): resource_creation_succeeded,
    (
        models.Order.Types.CREATE,
        models.Order.States.ERRED,
    ): resource_creation_failed,
    (
        models.Order.Types.CREATE,
        models.Order.States.CANCELED,
    ): resource_creation_canceled,
    (
        models.Order.Types.UPDATE,
        models.Order.States.DONE,
    ): resource_update_succeeded,
    (
        models.Order.Types.UPDATE,
        models.Order.States.ERRED,
    ): resource_update_failed,
    (
        models.Order.Types.UPDATE,
        models.Order.States.CANCELED,
    ): resource_update_canceled,
    (
        models.Order.Types.TERMINATE,
        models.Order.States.DONE,
    ): resource_deletion_succeeded,
    (
        models.Order.Types.TERMINATE,
        models.Order.States.ERRED,
    ): resource_deletion_failed,
    (
        models.Order.Types.TERMINATE,
        models.Order.States.CANCELED,
    ): resource_deletion_canceled,
}


def sync_resource_state(instance, resource):
    key = (instance.tracker.previous('state'), instance.state)
    func = StateRouter.get(key)
    if func:
        func(resource)


def sync_order_state(order, new_state):
    key = (order.type, new_state)
    func = OrderHandlers.get(key)
    if func:
        func(order.resource)
