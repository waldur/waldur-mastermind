import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from waldur_core.checklist.models import ChecklistCompletion
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import ChangeEmailRequest, StateMixin
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import get_customer, get_permissions
from waldur_core.structure.managers import count_customer_users
from waldur_core.structure.models import (
    AccessSubnet,
    BaseResource,
    Customer,
    Project,
    ServiceSettings,
)

from . import tasks

logger = logging.getLogger(__name__)


def change_users_quota(sender, instance: UserRole, **kwargs):
    """Update the user count quota for a customer when a user's role is changed."""
    # Skip synchronization of custom roles
    if not instance.role.is_system_role:
        return

    if not isinstance(instance.scope, Customer | Project):
        return

    customer = get_customer(instance.scope)
    customer.set_quota_usage("nc_user_count", count_customer_users(customer))


def revoke_roles_on_project_deletion(sender, instance: Project | None = None, **kwargs):
    """
    When project is deleted, capture user role snapshots before revoking them.
    All project permissions are cascade deleted by Django without emitting role_revoked signal.
    So in order to invalidate nc_user_count quota we need to emit it manually.
    """

    # Get current user from request context if available
    current_user = getattr(instance, "_terminating_user", None)

    # 1. Capture current state of all active project roles
    active_roles = get_permissions(instance)

    # 2. Build termination metadata
    user_roles_data = []
    for role in active_roles:
        user_roles_data.append(
            {
                "user_username": role.user.username,
                "user_first_name": role.user.first_name,
                "user_last_name": role.user.last_name,
                "user_email": role.user.email,
                "role_name": role.role.name,
                "created_by_username": role.created_by.username
                if role.created_by
                else None,
                "original_created": role.created.isoformat(),
                "original_expiration_time": role.expiration_time.isoformat()
                if role.expiration_time
                else None,
                "is_restored": False,
                "restored_at": None,
                "restored_by": None,
            }
        )

    # 3. Store metadata in project if there are roles to capture
    if user_roles_data:
        instance.termination_metadata = {
            "terminated_at": timezone.now().isoformat(),
            "terminated_by": current_user.username if current_user else None,
            "terminated_by_first_name": current_user.first_name
            if current_user
            else None,
            "terminated_by_last_name": current_user.last_name if current_user else None,
            "terminated_by_email": current_user.email if current_user else None,
            "user_roles": user_roles_data,
        }
        instance.save(update_fields=["termination_metadata"])

    # 4. Revoke roles as before
    for permission in active_roles:
        permission.revoke(reason="Project deletion cascade")


def log_customer_save(sender, instance: Customer, created=False, **kwargs):
    """Log customer creation and updates."""
    if created:
        event_logger.emit(
            "Customer {customer_name} has been created.",
            event_type=EventType.CUSTOMER_CREATION_SUCCEEDED,
            event_context={
                "customer": instance,
            },
            scopes=[instance],
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

        event_logger.emit(
            message,
            event_type=EventType.CUSTOMER_UPDATE_SUCCEEDED,
            event_context={
                "customer": instance,
            },
            scopes=[instance],
        )


def log_customer_delete(sender, instance: Customer, **kwargs):
    """Log customer deletion."""
    event_logger.emit(
        "Customer {customer_name} has been deleted.",
        event_type=EventType.CUSTOMER_DELETION_SUCCEEDED,
        event_context={
            "customer": instance,
        },
        scopes=[instance],
    )


def log_project_save(sender, instance: Project, created=False, **kwargs):
    """Log project creation and updates."""
    if created:
        event_logger.emit(
            "Project {project_name} has been created.",
            event_type=EventType.PROJECT_CREATION_SUCCEEDED,
            event_context={
                "project": instance,
            },
            scopes=[instance, instance.customer],
        )
    else:
        changed_fields = instance.tracker.changed().copy()
        changed_fields.pop("modified", None)
        changed_fields.pop(
            "termination_metadata", None
        )  # Exclude JSON field from logging
        if not changed_fields:
            return

        message = "Project {project_name} has been updated."
        for name in sorted(changed_fields.keys()):
            previous_value = changed_fields[name]
            current_value = getattr(instance, name)
            message = f"{message} {name.capitalize()} has been changed from '{previous_value}' to '{current_value}'."

        event_logger.emit(
            message,
            event_type=EventType.PROJECT_UPDATE_SUCCEEDED,
            event_context={"project": instance},
            scopes=[instance, instance.customer],
        )


def log_project_delete(sender, instance: Project, **kwargs):
    """Log project deletion."""
    event_logger.emit(
        "Project {project_name} has been deleted.",
        event_type=EventType.PROJECT_DELETION_SUCCEEDED,
        event_context={
            "project": instance,
        },
        scopes=[instance, instance.customer],
    )


def log_resource_deleted(sender, instance: BaseResource, **kwargs):
    """Log resource deletion."""
    event_logger.emit(
        "{resource_full_name} has been deleted.",
        event_type=EventType.RESOURCE_DELETION_SUCCEEDED,
        event_context={"resource": instance},
        scopes=[instance, instance.project, instance.project.customer],
    )


def log_resource_imported(sender, instance: BaseResource, **kwargs):
    """Log resource import."""
    if not instance.pk:
        return
    event_logger.emit(
        "Resource {resource_full_name} has been imported.",
        event_type=EventType.RESOURCE_IMPORT_SUCCEEDED,
        event_context={"resource": instance},
        scopes=[instance, instance.project, instance.project.customer],
    )


def log_resource_creation_succeeded(instance: BaseResource):
    """Log successful resource creation."""
    event_logger.emit(
        "Resource {resource_name} has been created.",
        event_type=EventType.RESOURCE_CREATION_SUCCEEDED,
        event_context={"resource": instance},
        scopes=[instance, instance.project, instance.project.customer],
    )


def log_resource_creation_failed(instance: BaseResource):
    """Log failed resource creation."""
    event_logger.emit(
        "Resource {resource_name} creation has failed.",
        event_type=EventType.RESOURCE_CREATION_FAILED,
        event_context={"resource": instance},
        scopes=[instance, instance.project, instance.project.customer],
        level="error",
    )


def log_resource_creation_scheduled(
    sender, instance: BaseResource, created=False, **kwargs
):
    """Log scheduled resource creation."""
    if (
        created
        and isinstance(instance, StateMixin)
        and instance.state == CoreStates.CREATION_SCHEDULED
    ):
        transaction.on_commit(lambda: _log_resource_creation_scheduled(instance))


def _log_resource_creation_scheduled(instance: BaseResource):
    if instance.pk:
        event_logger.emit(
            "Resource {resource_name} creation has been scheduled.",
            event_type=EventType.RESOURCE_CREATION_SCHEDULED,
            event_context={"resource": instance},
            scopes=[instance, instance.project, instance.project.customer],
        )


def log_resource_action(sender, instance: BaseResource, name, source, target, **kwargs):
    """Log resource state transitions."""
    if isinstance(instance, StateMixin):
        if source == CoreStates.CREATING:
            if target == CoreStates.OK:
                log_resource_creation_succeeded(instance)
            elif target == CoreStates.ERRED:
                log_resource_creation_failed(instance)

    if isinstance(instance, StateMixin) and target == CoreStates.DELETION_SCHEDULED:
        event_logger.emit(
            "Resource {resource_name} deletion has been scheduled.",
            event_type=EventType.RESOURCE_DELETION_SCHEDULED,
            event_context={"resource": instance},
            scopes=[instance, instance.project, instance.project.customer],
        )


def generate_access_subnet_changes(instance: AccessSubnet, created=False):
    """Generate a string describing the changes to an access subnet."""
    changed_dict = instance.tracker.changed()
    changes_string = f"Access subnet {instance} has been updated.\n"
    for key, value in changed_dict.items():
        changes_string += (
            f"{key} has been changed from '{value}' to '{getattr(instance, key)}'. "
        )
    return changes_string


def log_access_subnet_update_succeeded(instance: AccessSubnet):
    """Log successful access subnet updates."""
    changes = generate_access_subnet_changes(instance)
    event_logger.emit(
        changes,
        event_type=EventType.ACCESS_SUBNET_UPDATE_SUCCEEDED,
        event_context={"access_subnet": instance},
        scopes=[instance, instance.customer],
    )


def log_access_subnet_creation_succeeded(instance: AccessSubnet):
    """Log successful access subnet creation."""
    event_logger.emit(
        f"Access subnet {instance} has been created.",
        event_type=EventType.ACCESS_SUBNET_CREATION_SUCCEEDED,
        event_context={"access_subnet": instance},
        scopes=[instance, instance.customer],
    )


def log_access_subnet_deletion_succeeded(sender, instance: AccessSubnet, **kwargs):
    """Log successful access subnet deletion."""
    event_logger.emit(
        f"Access subnet {instance} has been deleted.",
        event_type=EventType.ACCESS_SUBNET_DELETION_SUCCEEDED,
        event_context={"access_subnet": instance},
        scopes=[instance, instance.customer],
    )


def log_access_subnet_save(sender, instance: AccessSubnet, created=False, **kwargs):
    """Log access subnet creation and updates."""
    if created:
        log_access_subnet_creation_succeeded(instance)
    else:
        log_access_subnet_update_succeeded(instance)


def update_resource_start_time(sender, instance, created=False, **kwargs):
    """Update the start time of a resource when its runtime state changes."""
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
    """Update the user count for all customers."""
    for customer in Customer.objects.all():
        usage = count_customer_users(customer)
        customer.set_quota_usage("nc_user_count", usage)


def change_email_has_been_requested(
    sender, instance: ChangeEmailRequest, created=False, **kwargs
):
    """Send a notification when a user requests to change their email."""
    if not created:
        return

    request_serialized = core_utils.serialize_instance(instance)
    transaction.on_commit(
        lambda: tasks.send_change_email_notification.delay(request_serialized)
    )


def permissions_request_approved(sender, permission, structure, **kwargs):
    """Send a notification when a permission request has been approved."""
    permission_serialized = core_utils.serialize_instance(permission)
    structure_serialized = core_utils.serialize_instance(structure)
    transaction.on_commit(
        lambda: tasks.send_structure_role_granted_notification.delay(
            permission_serialized, structure_serialized
        )
    )


# Project metadata handlers
def create_project_metadata_completion(sender, instance, created=False, **kwargs):
    """Create ChecklistCompletion for project metadata when a project is created."""
    if not created:
        return

    project = instance

    # Check if customer has project metadata checklist configured
    if project.customer.project_metadata_checklist:
        # Create ChecklistCompletion using generic foreign key
        ChecklistCompletion.objects.create(
            checklist=project.customer.project_metadata_checklist,
            scope=project,
        )


def create_existing_projects_completions(sender, instance, **kwargs):
    """Create ChecklistCompletion for existing projects when customer checklist is updated."""
    customer = instance

    # Only create completions if a checklist is now set
    if not customer.project_metadata_checklist:
        return

    # Create completions for all existing projects under this customer
    project_content_type = ContentType.objects.get_for_model(Project)

    for project in customer.projects.all():
        # Only create if it doesn't exist yet - using traditional lookup for get_or_create
        completion, created = ChecklistCompletion.objects.get_or_create(
            checklist=customer.project_metadata_checklist,
            scope_content_type=project_content_type,
            scope_object_id=project.id,
        )


def delete_project_metadata_completions(sender, instance, **kwargs):
    """Delete ChecklistCompletions when customer's project metadata checklist is removed."""
    customer = instance

    # Check if project_metadata_checklist field has changed
    if customer.tracker.has_changed("project_metadata_checklist"):
        old_checklist = customer.tracker.previous("project_metadata_checklist")
        current_checklist = customer.project_metadata_checklist

        # If checklist was removed or changed, delete old completions
        if old_checklist and old_checklist != current_checklist:
            project_content_type = ContentType.objects.get_for_model(Project)
            project_ids = list(customer.projects.values_list("id", flat=True))

            ChecklistCompletion.objects.filter(
                checklist=old_checklist,
                scope_content_type=project_content_type,
                scope_object_id__in=project_ids,
            ).delete()
