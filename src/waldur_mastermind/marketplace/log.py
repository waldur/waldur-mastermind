from django.conf import settings
from django.db import transaction

from waldur_core.logging.loggers import EventLogger, event_logger

from . import models, tasks


class MarketplaceOrderLogger(EventLogger):
    order = models.Order

    class Meta:
        event_types = (
            'marketplace_order_created',
            'marketplace_order_approved',
            'marketplace_order_rejected',
            'marketplace_order_completed',
            'marketplace_order_terminated',
            'marketplace_order_failed',
        )


class MarketplaceResourceLogger(EventLogger):
    resource = models.Resource

    def process(self, level, message_template, event_type='undefined', event_context=None):
        super(MarketplaceResourceLogger, self).process(level, message_template, event_type, event_context)

        if not event_context:
            event_context = {}

        if not settings.WALDUR_MARKETPLACE['NOTIFY_ABOUT_RESOURCE_CHANGE'] or event_type not in (
                'marketplace_resource_create_succeeded',
                'marketplace_resource_create_failed',
                'marketplace_resource_update_succeeded',
                'marketplace_resource_update_failed',
                'marketplace_resource_terminate_succeeded',
                'marketplace_resource_terminate_failed',):
            return

        if (settings.WALDUR_MARKETPLACE['DISABLE_SENDING_NOTIFICATIONS_ABOUT_RESOURCE_UPDATE'] and
                event_type == 'marketplace_resource_update_succeeded'):
            return

        context = self.compile_context(**event_context)
        resource = event_context['resource']
        transaction.on_commit(lambda: tasks.notify_about_resource_change.delay(event_type, context, resource.uuid))

    class Meta:
        event_types = (
            'marketplace_resource_create_requested',
            'marketplace_resource_create_succeeded',
            'marketplace_resource_create_failed',
            'marketplace_resource_update_requested',
            'marketplace_resource_update_succeeded',
            'marketplace_resource_update_failed',
            'marketplace_resource_terminate_requested',
            'marketplace_resource_terminate_succeeded',
            'marketplace_resource_terminate_failed',
        )


event_logger.register('marketplace_order', MarketplaceOrderLogger)
event_logger.register('marketplace_resource', MarketplaceResourceLogger)


def log_order_created(order):
    event_logger.marketplace_order.info(
        'Marketplace order has been created.',
        event_type='marketplace_order_created',
        event_context={'order': order},
    )


def log_order_approved(order):
    event_logger.marketplace_order.info(
        'Marketplace order has been approved.',
        event_type='marketplace_order_approved',
        event_context={'order': order},
    )


def log_order_rejected(order):
    event_logger.marketplace_order.info(
        'Marketplace order has been rejected.',
        event_type='marketplace_order_rejected',
        event_context={'order': order},
    )


def log_order_completed(order):
    event_logger.marketplace_order.info(
        'Marketplace order has been completed.',
        event_type='marketplace_order_completed',
        event_context={'order': order},
    )


def log_order_terminated(order):
    event_logger.marketplace_order.info(
        'Marketplace order has been terminated.',
        event_type='marketplace_order_terminated',
        event_context={'order': order},
    )


def log_order_failed(order):
    event_logger.marketplace_order.info(
        'Marketplace order has been marked as failed.',
        event_type='marketplace_order_failed',
        event_context={'order': order},
    )


def log_resource_creation_requested(resource):
    event_logger.marketplace_resource.info(
        'Resource {resource_name} creation has been requested.',
        event_type='marketplace_resource_create_requested',
        event_context={'resource': resource},
    )


def log_resource_creation_succeeded(resource):
    event_logger.marketplace_resource.info(
        'Resource {resource_name} has been created.',
        event_type='marketplace_resource_create_succeeded',
        event_context={'resource': resource},
    )


def log_resource_creation_failed(instance):
    event_logger.marketplace_resource.error(
        'Resource {resource_name} creation has failed.',
        event_type='marketplace_resource_create_failed',
        event_context={'resource': instance},
    )


def log_resource_update_requested(resource):
    event_logger.marketplace_resource.info(
        'Resource {resource_name} update has been requested.',
        event_type='marketplace_resource_update_requested',
        event_context={'resource': resource},
    )


def log_resource_update_succeeded(resource):
    event_logger.marketplace_resource.info(
        'Resource {resource_name} has been updated.',
        event_type='marketplace_resource_update_succeeded',
        event_context={'resource': resource},
    )


def log_resource_update_failed(instance):
    event_logger.marketplace_resource.error(
        'Resource {resource_name} update has failed.',
        event_type='marketplace_resource_update_failed',
        event_context={'resource': instance},
    )


def log_resource_terminate_requested(resource):
    event_logger.marketplace_resource.info(
        'Resource {resource_name} deletion been requested.',
        event_type='marketplace_resource_terminate_requested',
        event_context={'resource': resource},
    )


def log_resource_terminate_succeeded(resource):
    event_logger.marketplace_resource.info(
        'Resource {resource_name} has been deleted.',
        event_type='marketplace_resource_terminate_succeeded',
        event_context={'resource': resource},
    )


def log_resource_terminate_failed(instance):
    event_logger.marketplace_resource.error(
        'Resource {resource_name} deletion has failed.',
        event_type='marketplace_resource_terminate_failed',
        event_context={'resource': instance},
    )
