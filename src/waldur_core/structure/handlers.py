import logging

from django.db import transaction
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import StateMixin
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import get_customer, get_permissions
from waldur_core.structure.log import event_logger
from waldur_core.structure.managers import count_customer_users
from waldur_core.structure.models import Customer, Project, ServiceSettings

from . import tasks

logger = logging.getLogger(__name__)


def change_users_quota(sender, instance: UserRole, **kwargs):
    # Skip synchronization of custom roles
    if not instance.role.is_system_role:
        return

    if not isinstance(instance.scope, Customer | Project):
        return

    customer = get_customer(instance.scope)
    customer.set_quota_usage("nc_user_count", count_customer_users(customer))


def revoke_roles_on_project_deletion(sender, instance=None, **kwargs):
    """
    When project is deleted, all project permissions are cascade deleted
    by Django without emitting role_revoked signal.
    So in order to invalidate nc_user_count quota we need to emit it manually.
    """
    for permission in get_permissions(instance):
        permission.revoke()


def log_customer_save(sender, instance, created=False, **kwargs):
    if created:
        event_logger.customer.info(
            "Customer {customer_name} has been created.",
            event_type="customer_creation_succeeded",
            event_context={
                "customer": instance,
            },
        )
    else:
        changed_fields = instance.tracker.changed().copy()
        for field in (
            "modified",
            "image",
            "access_subnet_set",
            "projects",
            "reviews",
            "service_settings",
            "groupinvitation",
            "invitation",
            "customeropenstack",
            "customercluster",
            "customernetwork",
            "customernetworkpair",
            "customerdatastore",
            "customerfolder",
            "paymentprofile",
            "customercredit",
            "serviceprovider",
            "checklist",
            "customerestimatedcostpolicy",
            "callmanagingorganisation",
            "issues",
            "organization_groups",
        ):
            changed_fields.pop(field, None)

        if not changed_fields:
            return
        message = "Customer {customer_name} has been updated."

        for name in sorted(changed_fields.keys()):
            previous_value = changed_fields[name]
            current_value = getattr(instance, name)
            message = f"{message} {name.capitalize()} has been changed from '{previous_value}' to '{current_value}'."

        event_logger.customer.info(
            message,
            event_type="customer_update_succeeded",
            event_context={
                "customer": instance,
            },
        )


def log_customer_delete(sender, instance, **kwargs):
    event_logger.customer.info(
        "Customer {customer_name} has been deleted.",
        event_type="customer_deletion_succeeded",
        event_context={
            "customer": instance,
        },
    )


def log_project_save(sender, instance, created=False, **kwargs):
    if created:
        event_logger.project.info(
            "Project {project_name} has been created.",
            event_type="project_creation_succeeded",
            event_context={
                "project": instance,
            },
        )
    else:
        changed_fields = instance.tracker.changed().copy()
        changed_fields.pop("modified", None)
        if not changed_fields:
            return

        message = "Project {project_name} has been updated."
        for name in sorted(changed_fields.keys()):
            previous_value = changed_fields[name]
            current_value = getattr(instance, name)
            message = f"{message} {name.capitalize()} has been changed from '{previous_value}' to '{current_value}'."

        event_logger.project.info(
            message,
            event_type="project_update_succeeded",
            event_context={"project": instance},
        )


def log_project_delete(sender, instance, **kwargs):
    event_logger.project.info(
        "Project {project_name} has been deleted.",
        event_type="project_deletion_succeeded",
        event_context={
            "project": instance,
        },
    )


def log_resource_deleted(sender, instance, **kwargs):
    event_logger.resource.info(
        "{resource_full_name} has been deleted.",
        event_type="resource_deletion_succeeded",
        event_context={"resource": instance},
    )


def log_resource_imported(sender, instance, **kwargs):
    if not instance.pk:
        return
    event_logger.resource.info(
        "Resource {resource_full_name} has been imported.",
        event_type="resource_import_succeeded",
        event_context={"resource": instance},
    )


def log_resource_creation_succeeded(instance):
    event_logger.resource.info(
        "Resource {resource_name} has been created.",
        event_type="resource_creation_succeeded",
        event_context={"resource": instance},
    )


def log_resource_creation_failed(instance):
    event_logger.resource.error(
        "Resource {resource_name} creation has failed.",
        event_type="resource_creation_failed",
        event_context={"resource": instance},
    )


def log_resource_creation_scheduled(sender, instance, created=False, **kwargs):
    if (
        created
        and isinstance(instance, StateMixin)
        and instance.state == CoreStates.CREATION_SCHEDULED
    ):
        transaction.on_commit(lambda: _log_resource_creation_scheduled(instance))


def _log_resource_creation_scheduled(instance):
    if instance.pk:
        event_logger.resource.info(
            "Resource {resource_name} creation has been scheduled.",
            event_type="resource_creation_scheduled",
            event_context={"resource": instance},
        )


def log_resource_action(sender, instance, name, source, target, **kwargs):
    if isinstance(instance, StateMixin):
        if source == CoreStates.CREATING:
            if target == CoreStates.OK:
                log_resource_creation_succeeded(instance)
            elif target == CoreStates.ERRED:
                log_resource_creation_failed(instance)

    if isinstance(instance, StateMixin) and target == CoreStates.DELETION_SCHEDULED:
        event_logger.resource.info(
            "Resource {resource_name} deletion has been scheduled.",
            event_type="resource_deletion_scheduled",
            event_context={"resource": instance},
        )


def generate_access_subnet_changes(instance, created=False):
    changed_dict = instance.tracker.changed()
    changes_string = f"Access subnet {instance} has been updated.\n"
    for key, value in changed_dict.items():
        changes_string += (
            f"{key} has been changed from '{value}' to '{getattr(instance, key)}'. "
        )
    return changes_string


def log_access_subnet_update_succeeded(instance):
    changes = generate_access_subnet_changes(instance)
    event_logger.access_subnet.info(
        changes,
        event_type="access_subnet_update_succeeded",
        event_context={"access_subnet": instance},
    )


def log_access_subnet_creation_succeeded(instance):
    event_logger.access_subnet.info(
        f"Access subnet {instance} has been created.",
        event_type="access_subnet_creation_succeeded",
        event_context={"access_subnet": instance},
    )


def log_access_subnet_deletion_succeeded(sender, instance, **kwargs):
    event_logger.access_subnet.info(
        f"Access subnet {instance} has been deleted.",
        event_type="access_subnet_deletion_succeeded",
        event_context={"access_subnet": instance},
    )


def log_access_subnet_save(sender, instance, created=False, **kwargs):
    if created:
        log_access_subnet_creation_succeeded(instance)
    else:
        log_access_subnet_update_succeeded(instance)


def update_resource_start_time(sender, instance, created=False, **kwargs):
    if created:
        return

    if not instance.tracker.has_changed("runtime_state"):
        return

    # queryset is needed in order to call update method which does not
    # emit post_save signal, otherwise it's called recursively
    queryset = instance._meta.model.objects.filter(pk=instance.pk)

    if instance.runtime_state == instance.get_online_state():
        queryset.update(start_time=timezone.now())

    if instance.runtime_state == instance.get_offline_state():
        queryset.update(start_time=None)


def delete_service_settings_on_scope_delete(sender, instance, **kwargs):
    """If VM that contains service settings were deleted - all settings
    resources could be safely deleted from Waldur.
    """
    for service_settings in ServiceSettings.objects.filter(scope=instance):
        service_settings.delete()


def update_customer_users_count(sender, **kwargs):
    for customer in Customer.objects.all():
        usage = count_customer_users(customer)
        customer.set_quota_usage("nc_user_count", usage)


def change_email_has_been_requested(sender, instance, created=False, **kwargs):
    if not created:
        return

    request_serialized = core_utils.serialize_instance(instance)
    transaction.on_commit(
        lambda: tasks.send_change_email_notification.delay(request_serialized)
    )


def permissions_request_approved(sender, permission, structure, **kwargs):
    permission_serialized = core_utils.serialize_instance(permission)
    structure_serialized = core_utils.serialize_instance(structure)
    transaction.on_commit(
        lambda: tasks.send_structure_role_granted_notification.delay(
            permission_serialized, structure_serialized
        )
    )
