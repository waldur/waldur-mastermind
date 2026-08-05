import functools
import logging
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from waldur_core.core import utils as core_utils
from waldur_core.core.models import User
from waldur_core.permissions.models import UserRole
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.marketplace import models as marketplace_models

from . import models, tasks, utils

logger = logging.getLogger(__name__)


def if_plugin_enabled(f):
    """Calls decorated handler only if plugin is enabled."""

    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        if settings.WALDUR_OPENPORTAL["ENABLED"]:
            return f(*args, **kwargs)
        else:
            logger.debug("Skipping OpenPortal handler because plugin is disabled.")

    return wrapped


@if_plugin_enabled
def schedule_sync(*args, **kwargs):
    """
    Schedule a synchronization of OpenPortal data. This will double-check
    that all users are correctly associated with all projects and
    instances, and will add/remove users as needed
    """
    logger.info("Scheduling OpenPortal synchronization.")

    # We will rate-limit by synchronizing only once per transaction.
    # This is useful for when multiple changes are made in a single transaction,
    # such as when a user is created and then immediately added to a project.
    if transaction.get_connection().in_atomic_block:
        logger.info("OpenPortal synchronization already scheduled in this transaction.")
        return

    transaction.on_commit(lambda: tasks.sync.delay())


@if_plugin_enabled
def schedule_project_sync(project):
    """
    Schedule a synchronization of OpenPortal data for a specific project.
    This will double-check that all users are correctly associated with
    the project, and will add/remove users as needed
    """

    logger.info(f"Scheduling OpenPortal synchronization for project {project}")
    transaction.on_commit(
        lambda: tasks.sync_project.delay(core_utils.serialize_instance(project))
    )


@if_plugin_enabled
def update_allocation_credits(sender, instance, force_update=False, **kwargs):
    """
    Update the allocation credits for the given resource. This is called
    when a resource is created or updated, and will ensure that the
    allocation credits are correctly set in OpenPortal.
    """
    resource = instance

    logger.debug(f"OpenPortal - task update_allocation_credits {resource}")

    if not isinstance(resource, marketplace_models.Resource):
        logger.error(
            f"OpenPortal - {resource} is not a Resource instance - it is {type(resource)}"
        )
        return

    # Only update if the 'options' have changed - these contain the
    # allocation that must be communicated to the remote project
    if force_update or set(resource.tracker.changed()) & {
        "options",
    }:
        # Check to see if there is a remote allocation associated with this resource
        uuid = str(resource.uuid)

        logger.debug(f"OpenPortal - checking remote allocations for resource {uuid}")

        # Track projects that need credit sync
        projects_to_sync = set()

        for remote_allocation in models.RemoteAllocation.objects.filter(is_active=True):
            if remote_allocation.marketplace_uuid == uuid:
                project = remote_allocation.project

                if not project.is_expired or project.is_removed:
                    logger.debug(
                        f"OpenPortal.update_allocation_credits - updating project {project}"
                    )

                    transaction.on_commit(
                        lambda: tasks.update_remote_project.delay(
                            core_utils.serialize_instance(project)
                        )
                    )

                    # Mark this project for credit sync
                    projects_to_sync.add(project)

        # Sync project credits for all affected projects
        for project in projects_to_sync:
            _sync_project_credits_for_project(project, created=False)


@if_plugin_enabled
def update_project(sender, instance, force_add=False, **kwargs):
    """
    Update the project (passed as "instance") in OpenPortal. If this
    is a remote project, then it will send the OpenPortal remote
    commands to update the project in the remote portal
    """
    project = instance

    logger.debug(f"OpenPortal - task update_project {project}")
    logger.debug(
        f"OpenPortal - force_add={force_add}, changed={project.tracker.changed()}"
    )

    if force_add or set(project.tracker.changed()) & {
        "name",
        "description",
        "start_date",
        "end_date",
    }:
        # check to see if there are any remote allocations associated
        # with this project
        remote_allocations = models.RemoteAllocation.objects.filter(
            project=project,
        )

        if not remote_allocations.exists():
            # nothing to do
            return

        # Either the project's name or description has changed, or the
        # project has just been added to the user - we need to update the
        # project (updating is the same as adding in OpenPortal)
        logger.debug(f"OpenPortal - updating project {project}")

        transaction.on_commit(
            lambda: tasks.update_remote_project.delay(
                core_utils.serialize_instance(project)
            )
        )


@if_plugin_enabled
def delete_project(sender, instance, **kwargs):
    """
    Make sure to remote the project in the remote portal if it is
    deleted from the top portal
    """
    project = instance

    # check to see if there are any remote allocations associated
    remote_allocations = models.RemoteAllocation.objects.filter(
        project=project,
    )

    if remote_allocations.exists():
        transaction.on_commit(
            lambda: tasks.delete_remote_project.delay(
                core_utils.serialize_instance(project)
            )
        )

    # Handle connected managed projects. If the managed project's allocation
    # period has already ended, notify the remote portal and delete it. If the
    # period is still in the future (or no end date is set), detach and mark as
    # needing approval so the site admin can re-attach it to a new project.
    managed_projects = models.ManagedProject.objects.filter(project=project)

    for managed_project in managed_projects:
        end_date = managed_project.get_details().end_date

        if end_date is not None and end_date <= date.today():
            managed_project.notify_removed()
            managed_project.delete()
            logger.debug(
                f"ManagedProject {managed_project.identifier} "
                f"deleted, remote notified (expired project: {project})"
            )
        else:
            managed_project.set_needs_approval(
                True,
                comment="Attached project was deleted.",
            )
            managed_project.project = None
            managed_project.save(update_fields=["project"])
            logger.debug(
                f"ManagedProject {managed_project.identifier}"
                f" marked as needing approval: project {project} deleted"
            )


@if_plugin_enabled
def update_user(sender, instance, force_add=False, **kwargs):
    """
    Update the user (passed as "instance") in OpenPortal. This will
    look for all associations between the user and all projects and
    will add the user to each of those projects.
    """

    user = instance

    if force_add or set(user.tracker.changed()) & {"unix_username"}:
        # Either the user's unix_username has changed, or the user has
        # just been added to the project - we need to update the user
        # (updating is the same as adding in OpenPortal)
        logger.debug(f"OpenPortal - updating user {user}")

        # check if this is a User type
        if not isinstance(user, User):
            logger.error(
                f"OpenPortal - {user} is not a User instance - it is {type(user)}"
            )
            return

        transaction.on_commit(
            lambda: tasks.update_user.delay(core_utils.serialize_instance(user))
        )


@if_plugin_enabled
def delete_user(sender, instance, **kwargs):
    """
    Completely delete the user from all OpenPortal allocations to which
    they are allocated. This is called when a user is completely deleted
    from Waldur. It should not be used to remove a user from an individual
    project. Instead, schedule a sync, and the sync will remove all users
    who are incorrectly still associated with projects.
    """
    user = instance

    if not isinstance(instance, User):
        logger.error(f"OpenPortal - {user} is not a User instance - it is {type(user)}")
        return

    transaction.on_commit(
        lambda: tasks.delete_user.delay(core_utils.serialize_instance(instance))
    )


@if_plugin_enabled
def role_granted(sender, instance: UserRole, **kwargs):
    """
    This function is called when a user is granted a role in a project.
    The instance should by a UserRole. Note that this will trigger
    an `update_user` for the user, which will ensure that the user
    is correctly added to all projects to which they should have access.
    """
    logger.debug(
        f"OpenPortal - granting role {instance.role} for user {instance.user} in {instance.scope}"
    )

    user = instance.user

    if not isinstance(user, User):
        logger.error(f"OpenPortal - {user} is not a User instance - it is {type(user)}")
        return

    if not user.is_active:
        logger.warning(
            f"Cannot synchronize role {instance.role} for user {instance.user} as user is not active."
        )
        return

    # Skip synchronization of custom roles
    if not instance.role.is_system_role:
        logger.warning(f"{instance.role} is not a system role - synchronising anyway")

    if not instance.role.is_active:
        logger.warning(
            f"Cannot synchronize role {instance.role} for user {instance.user} as role is not active."
        )
        return

    if not isinstance(instance.scope, Customer | Project):
        logger.warning(f"Skipping as {instance.scope} is not a Customer or Project")
        return

    # let's just update the user...
    logger.debug(
        f"Really sending update_user({sender}, {user}, force_add=True, **{kwargs})"
    )
    update_user(sender, user, force_add=True, **kwargs)


@if_plugin_enabled
def role_revoked(sender, instance, **kwargs):
    """
    Revoke a role from the passed user. Note that this will trigger a full
    OpenPortal synchronization, which will ensure that all users are
    removed from projects from which they are not allocated.
    """
    logger.debug(
        f"OpenPortal - revoking role {instance.role} for user {instance.user} in {instance.scope}"
    )

    # Skip synchronization of custom roles
    if not instance.role.is_system_role:
        logger.warning(f"{instance.role} is not a system role - synchronising anyway")

    if not isinstance(instance.scope, Customer | Project):
        logger.warning(f"Skipping as {instance.scope} is not a Customer or Project")
        return

    # re-sync everyone in the project - this is safe as it will catch
    # people who should have been removed previously too
    schedule_project_sync(instance.scope)


@if_plugin_enabled
def update_quotas_on_allocation_usage_update(sender, instance, created=False, **kwargs):
    if created:
        return

    allocation = instance
    if not allocation.usage_changed():
        return

    logger.debug(f"OpenPortal - updating quotas for allocation {allocation}")

    project = allocation.project
    update_quotas(project, models.Allocation.Permissions.project_path)
    update_quotas(project.customer, models.Allocation.Permissions.customer_path)


@if_plugin_enabled
def update_quotas_on_remote_allocation_usage_update(
    sender, instance, created=False, **kwargs
):
    if created:
        return

    allocation = instance
    if not allocation.usage_changed():
        return

    logger.debug(f"OpenPortal - updating remote quotas for allocation {allocation}")

    project = allocation.project
    update_remote_quotas(project, models.RemoteAllocation.Permissions.project_path)
    update_remote_quotas(
        project.customer, models.RemoteAllocation.Permissions.customer_path
    )


def _sync_project_credits_for_project(project, created=False):
    """
    Helper function to synchronize ProjectCredit for a given project.
    Calculates the sum of all RemoteAllocation allocations and updates
    the project's credits accordingly.
    """
    if project is None:
        logger.warning("Project is None - cannot sync credits")
        return

    if project.is_expired or project.is_removed:
        logger.debug(f"Project {project} is expired or removed - skipping credit sync")
        return

    action = "creation" if created else "update"
    logger.debug(
        f"OpenPortal - syncing project credits for project {project} "
        f"due to allocation {action}"
    )

    # Calculate total allocation from all active RemoteAllocations
    try:
        total_allocation = Decimal(0)

        remote_allocations = models.RemoteAllocation.objects.filter(
            project=project, is_active=True
        )

        for allocation in remote_allocations:
            # Get the allocation value from the marketplace resource options
            try:
                (
                    allocation_value,
                    allocation_unit,
                ) = allocation._get_requested_allocation()
                if allocation_value is not None:
                    total_allocation += Decimal(str(allocation_value))
                    logger.debug(
                        f"RemoteAllocation {allocation}: "
                        f"{allocation_value} {allocation_unit}"
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to get allocation for RemoteAllocation {allocation}: {e}"
                )
                continue

        logger.debug(
            f"Total allocation for project {project}: {total_allocation} "
            f"(from {remote_allocations.count()} RemoteAllocations)"
        )

        # Set the project credits to match the total allocation
        if total_allocation > Decimal(0):
            utils.set_project_credits(project, total_allocation)
            logger.debug(f"Set project credits for {project} to {total_allocation}")
        else:
            logger.debug(
                f"No positive allocation found for project {project} - "
                "skipping credit update"
            )

    except Exception as e:
        logger.error(
            f"Failed to sync project credits for project {project}: {e}",
            exc_info=True,
        )


@if_plugin_enabled
def sync_project_credits_on_remote_allocation_change(
    sender, instance, created=False, **kwargs
):
    """
    Synchronize ProjectCredit when a RemoteAllocation is created or updated.
    This ensures that the local project's credits match the sum of all
    RemoteAllocation resources' allocations.
    """
    remote_allocation = instance
    project = remote_allocation.project

    if project is None:
        logger.warning(
            f"RemoteAllocation {remote_allocation} has no project - cannot sync credits"
        )
        return

    _sync_project_credits_for_project(project, created=created)


def update_quotas(scope, path):
    logger.debug(f"OpenPortal - updating quotas for {scope} at {path}")

    qs = models.Allocation.objects.filter(**{path: scope}).values(path)
    for quota in utils.FIELD_NAMES:
        qs = qs.annotate(**{"total_%s" % quota: Sum(quota)})
    qs = list(qs)[0]

    for quota in utils.FIELD_NAMES:
        scope.set_quota_usage(utils.MAPPING[quota], qs["total_%s" % quota])


def update_remote_quotas(scope, path):
    logger.debug(f"OpenPortal - updating remote quotas for {scope} at {path}")

    qs = models.RemoteAllocation.objects.filter(**{path: scope}).values(path)
    for quota in utils.FIELD_NAMES:
        qs = qs.annotate(**{"total_%s" % quota: Sum(quota)})
    qs = list(qs)[0]

    for quota in utils.FIELD_NAMES:
        scope.set_quota_usage(utils.MAPPING[quota], qs["total_%s" % quota])


def update_offering_agents(**kwargs):
    transaction.on_commit(lambda: tasks.sync_offering_agents.delay())
