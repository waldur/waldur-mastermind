import six

from waldur_core.core.models import User
from waldur_core.logging.loggers import EventLogger, event_logger
from waldur_core.structure import models


class CustomerEventLogger(EventLogger):
    customer = models.Customer

    class Meta:
        event_types = ('customer_deletion_succeeded',
                       'customer_update_succeeded',
                       'customer_creation_succeeded')
        event_groups = {
            'customers': event_types,
        }


class ProjectEventLogger(EventLogger):
    project = models.Project

    class Meta:
        event_types = ('project_deletion_succeeded',
                       'project_update_succeeded',
                       'project_creation_succeeded')
        event_groups = {
            'projects': event_types,
        }


class CustomerRoleEventLogger(EventLogger):
    customer = models.Customer
    affected_user = User
    user = User
    structure_type = six.text_type
    role_name = six.text_type

    class Meta:
        event_types = 'role_granted', 'role_revoked', 'role_updated'
        event_groups = {
            'customers': event_types,
            'users': event_types,
        }
        nullable_fields = ['user']


class ProjectRoleEventLogger(EventLogger):
    project = models.Project
    user = User
    affected_user = User
    structure_type = six.text_type
    role_name = six.text_type

    class Meta:
        event_types = 'role_granted', 'role_revoked', 'role_updated'
        event_groups = {
            'projects': event_types,
            'users': event_types,
        }
        nullable_fields = ['user']


class ResourceEventLogger(EventLogger):
    resource = models.ResourceMixin

    class Meta:
        event_types = (
            'resource_start_scheduled',
            'resource_start_succeeded',
            'resource_start_failed',

            'resource_stop_scheduled',
            'resource_stop_succeeded',
            'resource_stop_failed',

            'resource_restart_scheduled',
            'resource_restart_succeeded',
            'resource_restart_failed',

            'resource_creation_scheduled',
            'resource_creation_succeeded',
            'resource_creation_failed',

            'resource_import_succeeded',
            'resource_update_succeeded',

            'resource_deletion_scheduled',
            'resource_deletion_succeeded',
            'resource_deletion_failed',
        )
        event_groups = {
            'resources': event_types,
        }


class ServiceProjectLinkEventLogger(EventLogger):
    spl = models.ServiceProjectLink

    class Meta:
        event_types = ('spl_deletion_succeeded',
                       'spl_creation_succeeded')


event_logger.register('customer_role', CustomerRoleEventLogger)
event_logger.register('project_role', ProjectRoleEventLogger)
event_logger.register('customer', CustomerEventLogger)
event_logger.register('project', ProjectEventLogger)
event_logger.register('resource', ResourceEventLogger)
event_logger.register('spl', ServiceProjectLinkEventLogger)
