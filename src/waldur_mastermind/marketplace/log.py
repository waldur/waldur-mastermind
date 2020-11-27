from django.conf import settings
from django.db import transaction

from waldur_core.core.models import User
from waldur_core.logging.loggers import EventLogger, event_logger
from waldur_core.structure.models import Project

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

    @staticmethod
    def get_scopes(event_context):
        order = event_context['order']
        project = Project.all_objects.get(
            id=order.project_id
        )  # handle case when project is already deleted
        return {order, project, project.customer}


class MarketplaceResourceLogger(EventLogger):
    resource = models.Resource

    def process(
        self, level, message_template, event_type='undefined', event_context=None
    ):
        super(MarketplaceResourceLogger, self).process(
            level, message_template, event_type, event_context
        )

        if not event_context:
            event_context = {}

        if not settings.WALDUR_MARKETPLACE[
            'NOTIFY_ABOUT_RESOURCE_CHANGE'
        ] or event_type not in (
            'marketplace_resource_create_succeeded',
            'marketplace_resource_create_failed',
            'marketplace_resource_create_canceled',
            'marketplace_resource_update_succeeded',
            'marketplace_resource_update_failed',
            'marketplace_resource_terminate_succeeded',
            'marketplace_resource_terminate_failed',
            'marketplace_resource_update_limits_succeeded',
            'marketplace_resource_update_limits_failed',
        ):
            return

        if (
            settings.WALDUR_MARKETPLACE[
                'DISABLE_SENDING_NOTIFICATIONS_ABOUT_RESOURCE_UPDATE'
            ]
            and event_type == 'marketplace_resource_update_succeeded'
        ):
            return

        context = self.compile_context(**event_context)
        resource = event_context['resource']
        transaction.on_commit(
            lambda: tasks.notify_about_resource_change.delay(
                event_type, context, resource.uuid
            )
        )

    class Meta:
        event_types = (
            'marketplace_resource_create_requested',
            'marketplace_resource_create_succeeded',
            'marketplace_resource_create_failed',
            'marketplace_resource_create_canceled',
            'marketplace_resource_update_requested',
            'marketplace_resource_update_succeeded',
            'marketplace_resource_update_failed',
            'marketplace_resource_terminate_requested',
            'marketplace_resource_terminate_succeeded',
            'marketplace_resource_terminate_failed',
            'marketplace_resource_update_limits_succeeded',
            'marketplace_resource_update_limits_failed',
        )

    @staticmethod
    def get_scopes(event_context):
        resource = event_context['resource']
        project = Project.all_objects.get(
            id=resource.project_id
        )  # handle case when project is already deleted
        return {resource, project, project.customer}


class MarketplaceComponentUsageLogger(EventLogger):
    usage = models.ComponentUsage

    class Meta:
        event_types = (
            'marketplace_component_usage_created',
            'marketplace_component_usage_updated',
        )

    @staticmethod
    def get_scopes(event_context):
        usage = event_context['usage']
        resource = usage.resource
        project = Project.all_objects.get(id=resource.project_id)
        return {resource, project, project.customer}


class MarketplaceOfferingPermissionEventLogger(EventLogger):
    offering = models.Offering
    affected_user = User
    user = User

    class Meta:
        event_types = 'role_granted', 'role_revoked', 'role_updated'
        event_groups = {
            'customers': event_types,
            'users': event_types,
        }
        nullable_fields = ['user']

    @staticmethod
    def get_scopes(event_context):
        return {event_context['offering'].customer}


event_logger.register('marketplace_order', MarketplaceOrderLogger)
event_logger.register('marketplace_resource', MarketplaceResourceLogger)
event_logger.register('marketplace_component_usage', MarketplaceComponentUsageLogger)
event_logger.register(
    'marketplace_offering_permission', MarketplaceOfferingPermissionEventLogger
)


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
        'Resource {resource_name} creation has been failed.',
        event_type='marketplace_resource_create_failed',
        event_context={'resource': instance},
    )


def log_resource_creation_canceled(instance):
    event_logger.marketplace_resource.info(
        'Resource {resource_name} creation has been canceled.',
        event_type='marketplace_resource_create_canceled',
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
        'Resource {resource_name} update has been failed.',
        event_type='marketplace_resource_update_failed',
        event_context={'resource': instance},
    )


def log_resource_terminate_requested(resource):
    event_logger.marketplace_resource.info(
        'Resource {resource_name} deletion has been requested.',
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
        'Resource {resource_name} deletion has been failed.',
        event_type='marketplace_resource_terminate_failed',
        event_context={'resource': instance},
    )


def log_resource_limit_update_succeeded(resource):
    event_logger.marketplace_resource.info(
        'Limits of resource {resource_name} have been updated.',
        event_type='marketplace_resource_update_limits_succeeded',
        event_context={'resource': resource},
    )


def log_resource_limit_update_failed(resource):
    event_logger.marketplace_resource.info(
        'Update limits of resource {resource_name} is failed.',
        event_type='marketplace_resource_update_limits_failed',
        event_context={'resource': resource},
    )


def log_component_usage_creation_succeeded(usage):
    event_logger.marketplace_component_usage.info(
        'Marketplace component usage {usage_uuid} has been created.',
        event_type='marketplace_component_usage_created',
        event_context={'usage': usage},
    )


def log_component_usage_update_succeeded(usage):
    event_logger.marketplace_component_usage.info(
        'Marketplace component usage {usage_uuid} has been updated.',
        event_type='marketplace_component_usage_updated',
        event_context={'usage': usage},
    )


def log_offering_permission_granted(offering, user, created_by=None):
    event_context = {
        'offering': offering,
        'affected_user': user,
    }
    if created_by:
        event_context['user'] = created_by

    event_logger.marketplace_offering_permission.info(
        'User {affected_user_username} has gained service manager permission in offering {offering_name}.',
        event_type='role_granted',
        event_context=event_context,
    )


def log_offering_permission_revoked(offering, user, removed_by=None):
    event_context = {
        'affected_user': user,
        'offering': offering,
    }
    if removed_by:
        event_context['user'] = removed_by

    event_logger.marketplace_offering_permission.info(
        'User {affected_user_username} has lost service manager permission in offering {offering_name}.',
        event_type='role_revoked',
        event_context=event_context,
    )


def log_offering_permission_updated(permission, user):
    template = (
        'User %(user_username)s has changed permission expiration time '
        'for user {affected_user_username} in offering {offering_name} from '
        '%(old_expiration_time)s to %(new_expiration_time)s.'
    )

    context = {
        'old_expiration_time': permission.tracker.previous('expiration_time'),
        'new_expiration_time': permission.expiration_time,
        'user_username': user.full_name or user.username,
    }

    event_context = {
        'affected_user': permission.user,
        'offering': permission.offering,
    }

    event_logger.marketplace_offering_permission.info(
        template % context, event_type='role_updated', event_context=event_context,
    )
