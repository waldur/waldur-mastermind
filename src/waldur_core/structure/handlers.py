from __future__ import unicode_literals

import logging
import re

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from waldur_core.core import utils
from waldur_core.core.models import StateMixin
from waldur_core.core.tasks import send_task
from waldur_core.structure import SupportedServices, signals
from waldur_core.structure.log import event_logger
from waldur_core.structure.models import (Customer, CustomerPermission, Project, ProjectPermission,
                                          Service, ServiceSettings, CustomerRole)

logger = logging.getLogger(__name__)


def revoke_roles_on_project_deletion(sender, instance=None, **kwargs):
    """
    When project is deleted, all project permissions are cascade deleted
    by Django without emitting structure_role_revoked signal.
    So in order to invalidate nc_user_count quota we need to emit it manually.
    """
    instance.remove_all_users()


def log_customer_save(sender, instance, created=False, **kwargs):
    if created:
        event_logger.customer.info(
            'Customer {customer_name} has been created.',
            event_type='customer_creation_succeeded',
            event_context={
                'customer': instance,
            })
    else:
        event_logger.customer.info(
            'Customer {customer_name} has been updated.',
            event_type='customer_update_succeeded',
            event_context={
                'customer': instance,
            })


def log_customer_delete(sender, instance, **kwargs):
    event_logger.customer.info(
        'Customer {customer_name} has been deleted.',
        event_type='customer_deletion_succeeded',
        event_context={
            'customer': instance,
        })


def log_project_save(sender, instance, created=False, **kwargs):
    if created:
        event_logger.project.info(
            'Project {project_name} has been created.',
            event_type='project_creation_succeeded',
            event_context={
                'project': instance,
            })
    else:
        changed_fields = instance.tracker.changed().copy()
        changed_fields.pop('modified', None)
        if not changed_fields:
            return

        message = 'Project {project_name} has been updated.'
        for name, previous_value in changed_fields.items():
            current_value = getattr(instance, name)
            message = "%s %s has been changed from '%s' to '%s'." % (
                message,
                name.capitalize(),
                previous_value,
                current_value,
            )

        event_logger.project.info(message, event_type='project_update_succeeded', event_context={'project': instance})


def log_project_delete(sender, instance, **kwargs):
    event_logger.project.info(
        'Project {project_name} has been deleted.',
        event_type='project_deletion_succeeded',
        event_context={
            'project': instance,
        })


def log_customer_role_granted(sender, structure, user, role, created_by=None, **kwargs):
    event_context = {
        'customer': structure,
        'affected_user': user,
        'structure_type': 'customer',
        'role_name': CustomerPermission(role=role).get_role_display(),
    }
    if created_by:
        event_context['user'] = created_by

    event_logger.customer_role.info(
        'User {affected_user_username} has gained role of {role_name} in customer {customer_name}.',
        event_type='role_granted', event_context=event_context)


def log_customer_role_revoked(sender, structure, user, role, removed_by=None, **kwargs):
    event_context = {
        'customer': structure,
        'affected_user': user,
        'structure_type': 'customer',
        'role_name': CustomerPermission(role=role).get_role_display(),
    }
    if removed_by:
        event_context['user'] = removed_by

    event_logger.customer_role.info(
        'User {affected_user_username} has lost role of {role_name} in customer {customer_name}.',
        event_type='role_revoked', event_context=event_context)


def log_customer_role_updated(sender, instance, user, **kwargs):
    template = 'User %(user_username)s has changed permission expiration time ' \
               'for user {affected_user_username} in customer {customer_name} from ' \
               '%(old_expiration_time)s to %(new_expiration_time)s.'

    context = {
        'old_expiration_time': instance.tracker.previous('expiration_time'),
        'new_expiration_time': instance.expiration_time,
        'user_username': user.full_name or user.username,
    }

    event_logger.customer_role.info(
        template % context,
        event_type='role_updated',
        event_context={
            'customer': instance.customer,
            'affected_user': instance.user,
            'structure_type': 'customer',
            'role_name': instance.get_role_display(),
        })


def log_project_role_granted(sender, structure, user, role, created_by=None, **kwargs):
    event_context = {
        'project': structure,
        'affected_user': user,
        'structure_type': 'project',
        'role_name': ProjectPermission(role=role).get_role_display(),
    }
    if created_by:
        event_context['user'] = created_by

    event_logger.project_role.info(
        'User {affected_user_username} has gained role of {role_name} in project {project_name}.',
        event_type='role_granted', event_context=event_context)


def log_project_role_revoked(sender, structure, user, role, removed_by=None, **kwargs):
    event_context = {
        'project': structure,
        'affected_user': user,
        'structure_type': 'project',
        'role_name': ProjectPermission(role=role).get_role_display()
    }
    if removed_by:
        event_context['user'] = removed_by

    event_logger.project_role.info(
        'User {affected_user_username} has revoked role of {role_name} in project {project_name}.',
        event_type='role_revoked', event_context=event_context)


def log_project_role_updated(sender, instance, user, **kwargs):
    template = 'User %(user_username)s has changed permission expiration time ' \
               'for user {affected_user_username} in project {project_name} from ' \
               '%(old_expiration_time)s to %(new_expiration_time)s.'

    context = {
        'old_expiration_time': instance.tracker.previous('expiration_time'),
        'new_expiration_time': instance.expiration_time,
        'user_username': user.full_name or user.username,
    }

    event_logger.project_role.info(
        template % context,
        event_type='role_updated',
        event_context={
            'project': instance.project,
            'affected_user': instance.user,
            'structure_type': 'project',
            'role_name': instance.get_role_display(),
        })


def change_customer_nc_users_quota(sender, structure, user, role, signal, **kwargs):
    """ Modify nc_user_count quota usage on structure role grant or revoke """
    assert signal in (signals.structure_role_granted, signals.structure_role_revoked), \
        'Handler "change_customer_nc_users_quota" has to be used only with structure_role signals'
    assert sender in (Customer, Project), \
        'Handler "change_customer_nc_users_quota" works only with Project and Customer models'

    if sender == Customer:
        customer = structure
    elif sender == Project:
        customer = structure.customer

    customer_users = customer.get_users()
    customer.set_quota_usage(Customer.Quotas.nc_user_count, customer_users.count())


def log_resource_deleted(sender, instance, **kwargs):
    event_logger.resource.info(
        '{resource_full_name} has been deleted.',
        event_type='resource_deletion_succeeded',
        event_context={'resource': instance})


def log_resource_imported(sender, instance, **kwargs):
    event_logger.resource.info(
        'Resource {resource_full_name} has been imported.',
        event_type='resource_import_succeeded',
        event_context={'resource': instance})


def log_resource_creation_succeeded(instance):
    event_logger.resource.info(
        'Resource {resource_name} has been created.',
        event_type='resource_creation_succeeded',
        event_context={'resource': instance})


def log_resource_creation_failed(instance):
    event_logger.resource.error(
        'Resource {resource_name} creation has failed.',
        event_type='resource_creation_failed',
        event_context={'resource': instance})


def log_resource_creation_scheduled(sender, instance, created=False, **kwargs):
    if created and isinstance(instance, StateMixin) and instance.state == StateMixin.States.CREATION_SCHEDULED:
        event_logger.resource.info(
            'Resource {resource_name} creation has been scheduled.',
            event_type='resource_creation_scheduled',
            event_context={'resource': instance},
        )


def log_resource_action(sender, instance, name, source, target, **kwargs):
    if isinstance(instance, StateMixin):
        if source == StateMixin.States.CREATING:
            if target == StateMixin.States.OK:
                log_resource_creation_succeeded(instance)
            elif target == StateMixin.States.ERRED:
                log_resource_creation_failed(instance)

    if isinstance(instance, StateMixin) and target == StateMixin.States.DELETION_SCHEDULED:
        event_logger.resource.info(
            'Resource {resource_name} deletion has been scheduled.',
            event_type='resource_deletion_scheduled',
            event_context={'resource': instance},
        )


def detect_vm_coordinates(sender, instance, name, source, target, **kwargs):
    # Check if geolocation is enabled
    if not settings.WALDUR_CORE.get('ENABLE_GEOIP', True):
        return

    # VM already has coordinates
    if instance.latitude is not None and instance.longitude is not None:
        return

    if target == StateMixin.States.OK:
        send_task('structure', 'detect_vm_coordinates')(utils.serialize_instance(instance))


def connect_customer_to_shared_service_settings(sender, instance, created=False, **kwargs):
    if not created:
        return
    customer = instance

    for shared_settings in ServiceSettings.objects.filter(shared=True):
        try:
            service_model = SupportedServices.get_service_models()[shared_settings.type]['service']
            service_model.objects.create(customer=customer,
                                         settings=shared_settings,
                                         available_for_all=True)
        except KeyError:
            logger.warning("Unregistered service of type %s" % shared_settings.type)


def connect_project_to_all_available_services(sender, instance, created=False, **kwargs):
    if not created:
        return
    project = instance

    for service_model in Service.get_all_models():
        for service in service_model.objects.filter(available_for_all=True, customer=project.customer):
            service_project_link_model = service.projects.through
            service_project_link_model.objects.create(project=project, service=service)


def connect_service_to_all_projects_if_it_is_available_for_all(sender, instance, created=False, **kwargs):
    service = instance
    if service.available_for_all:
        service_project_link_model = service.projects.through
        for project in service.customer.projects.all():
            service_project_link_model.objects.get_or_create(project=project, service=service)


def delete_service_settings_on_service_delete(sender, instance, **kwargs):
    """ Delete not shared service settings without services """
    service = instance
    try:
        service_settings = service.settings
    except ServiceSettings.DoesNotExist:
        # If this handler works together with delete_service_settings_on_scope_delete
        # it tries to delete service settings that are already deleted.
        return
    if not service_settings.shared:
        service.settings.delete()


def update_resource_start_time(sender, instance, created=False, **kwargs):
    if created:
        return

    if not instance.tracker.has_changed('runtime_state'):
        return

    # queryset is needed in order to call update method which does not
    # emit post_save signal, otherwise it's called recursively
    queryset = instance._meta.model.objects.filter(pk=instance.pk)

    if instance.runtime_state == instance.get_online_state():
        queryset.update(start_time=timezone.now())

    if instance.runtime_state == instance.get_offline_state():
        queryset.update(start_time=None)


def delete_service_settings_on_scope_delete(sender, instance, **kwargs):
    """ If VM that contains service settings were deleted - all settings
        resources could be safely deleted from NC.
    """
    for service_settings in ServiceSettings.objects.filter(scope=instance):
        service_settings.unlink_descendants()
        service_settings.delete()


def clean_tags_cache_after_tagged_item_saved(sender, instance, **kwargs):
    instance.content_object.clean_tag_cache()


def clean_tags_cache_before_tagged_item_deleted(sender, instance, **kwargs):
    instance.content_object.clean_tag_cache()


def notify_about_user_profile_changes(sender, instance, created=False, **kwargs):
    if created or not settings.WALDUR_CORE['NOTIFICATIONS_PROFILE_CHANGES']['ENABLED']:
        return

    user = instance
    change_fields = settings.WALDUR_CORE['NOTIFICATIONS_PROFILE_CHANGES']['FIELDS']
    organizations = Customer.objects.filter(
        permissions__user=user,
        permissions__is_active=True,
        permissions__role=CustomerRole.OWNER)

    if not ((set(change_fields) & set(user.tracker.changed())) and organizations):
        return

    fields = []
    for field in change_fields:
        if user.tracker.has_changed(field):
            fields.append({
                'name': field,
                'old_value': user.tracker.previous(field),
                'new_value': getattr(user, field, None)
            })

    msg = render_to_string('structure/notifications_profile_changes.html', {
        'user': user,
        'fields': fields,
        'organizations': organizations
    })

    msg = re.sub(r'\s+', ' ', msg).strip()

    event_logger.user.info(
        msg,
        event_type='user_profile_changed',
        event_context={'affected_user': user})


def update_customer_users_count(sender, **kwargs):
    for customer in Customer.objects.all():
        usage = len(set(customer.get_users()))
        customer.set_quota_usage(Customer.Quotas.nc_user_count, usage)


def log_spl_create(sender, instance, created=False, **kwargs):
    if created:
        event_logger.spl.info(
            'ServiceProjectLink for project \'{project_name}\' '
            '(service: \'{service_type}\', settings name: \'{settings_name}\', '
            'settings type: \'{service_settings_type}\') '
            'has been created.',
            event_type='spl_creation_succeeded',
            event_context={
                'spl': instance,
            })


def log_spl_delete(sender, instance, **kwargs):
    event_logger.spl.info(
        'ServiceProjectLink for project \'{project_name}\' '
        '(service: \'{service_type}\', settings name: \'{settings_name}\', '
        'settings type: \'{service_settings_type}\') '
        'has been deleted.',
        event_type='spl_deletion_succeeded',
        event_context={
            'spl': instance,
        })
