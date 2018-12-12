import logging

from django.core import exceptions as django_exceptions

from waldur_core.core.models import StateMixin

from . import models


logger = logging.getLogger(__name__)


def resource_creation_succeeded(resource):
    resource.set_state_ok()
    resource.save(update_fields=['state'])
    set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.CREATE,
        models.OrderItem.States.DONE,
    )


def resource_creation_failed(resource):
    resource.set_state_erred()
    resource.save(update_fields=['state'])
    set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.CREATE,
        models.OrderItem.States.ERRED,
    )


def resource_update_succeeded(resource):
    resource.set_state_ok()
    resource.save(update_fields=['state'])
    order_item = set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.UPDATE,
        models.OrderItem.States.DONE,
    )
    if order_item and order_item.plan:
        resource.plan = order_item.plan
        resource.save(update_fields=['plan'])


def resource_update_failed(resource):
    resource.set_state_erred()
    resource.save(update_fields=['state'])
    set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.UPDATE,
        models.OrderItem.States.ERRED,
    )


def resource_deletion_succeeded(resource):
    resource.set_state_terminated()
    resource.save(update_fields=['state'])
    set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.TERMINATE,
        models.OrderItem.States.DONE,
    )


def resource_deletion_failed(resource):
    resource.set_state_erred()
    resource.save(update_fields=['state'])
    set_order_item_state(
        resource,
        models.RequestTypeMixin.Types.TERMINATE,
        models.OrderItem.States.ERRED,
    )


def set_order_item_state(resource, type, new_state):
    try:
        order_item = models.OrderItem.objects.get(
            resource=resource,
            type=type,
            state=models.OrderItem.States.EXECUTING,
        )
    except django_exceptions.ObjectDoesNotExist:
        logger.debug('Skipping order item synchronization for marketplace resource '
                     'because order item is not found. Resource ID: %s', resource.id)
    except django_exceptions.MultipleObjectsReturned:
        logger.debug('Skipping order item synchronization for marketplace resource '
                     'because there are multiple active order items are found. '
                     'Resource ID: %s', resource.id)
    else:
        order_item.state = new_state
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


def sync_resource_state(instance, resource):
    key = (instance.tracker.previous('state'), instance.state)
    func = StateRouter.get(key)
    if func:
        func(resource)
