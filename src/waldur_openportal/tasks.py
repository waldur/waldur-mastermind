import datetime
import functools
import logging
import random
import time

import openportal
from celery import shared_task
from constance import config

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import User
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import backend, models, utils
from . import config as openportal_config
from .board import OpenPortalBoard

logger = logging.getLogger(__name__)


def run_once_task(takeover_timeout, include_args=False):
    """
    Decorator to ensure only one instance of a task runs at a time.

    Args:
        takeover_timeout: Timeout in seconds before a stale lock can be taken over
        include_args: If True, include positional arguments in the lock ID to create
                     per-argument locks (e.g., per-customer locks)
    """

    def task_exc(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Build lock_id based on function name and optionally arguments
            lock_id = "openportal-run-once-" + func.__name__
            if include_args and args:
                # Include positional arguments in lock ID for per-argument locking
                args_str = "-".join(str(arg) for arg in args)
                lock_id = f"{lock_id}-{args_str}"

            def acquire_lock():
                now = datetime.datetime.now(datetime.UTC)

                lock, created = models.OnceTask.objects.get_or_create(
                    task_name=lock_id,
                    defaults={"last_run": now},
                )

                if not created:
                    logger.warning(
                        f"OpenPortal lock {lock_id} already exists - checking takeover"
                    )

                    # someone else beat us to the lock - was this more than
                    # takeover_timeout seconds ago?
                    if (
                        lock.last_run is None
                        or (
                            now.replace(tzinfo=None)
                            - lock.last_run.replace(tzinfo=None)
                        ).seconds
                        > takeover_timeout
                    ):
                        # remove the lock
                        try:
                            lock.delete()
                        except Exception:
                            pass

                        # create a new lock
                        lock, created = models.OnceTask.objects.get_or_create(
                            task_name=lock_id,
                            defaults={"last_run": now},
                        )

                        if lock is None:
                            logger.error(
                                f"Failed to create OpenPortal lock {lock_id} - aborting"
                            )
                            return False
                        elif created:
                            logger.warning(
                                f"OpenPortal task {lock_id} takeover successful - running"
                            )
                            return True
                        else:
                            logger.info(
                                f"OpenPortal task {lock_id} already running - skipping"
                            )
                            return False
                    else:
                        logger.info(
                            f"OpenPortal task {lock_id} already running - skipping"
                        )
                        return False

                return True

            def release_lock():
                """
                Release the lock by deleting the OnceTask object with the given lock_id.
                """
                try:
                    lock = models.OnceTask.objects.get(task_name=lock_id)
                    lock.delete()
                except models.OnceTask.DoesNotExist:
                    logger.warning(
                        f"Lock {lock_id} does not exist - nothing to release"
                    )
                except Exception as e:
                    logger.error(f"Failed to release lock {lock_id}: {e}")

            if acquire_lock():
                try:
                    func(*args, **kwargs)
                finally:
                    release_lock()

        return wrapper

    return task_exc


def is_task_running(func):
    """
    Check if a task associated with the passed function is currently running.
    """
    try:
        lock_id = "openportal-run-once-" + func.__name__

        task = models.OnceTask.objects.get(task_name=lock_id)
        if task.last_run is not None:
            return True
    except models.OnceTask.DoesNotExist:
        return False
    return False


def get_structure_allocations(structure):
    """
    Return all of the allocations associated with the passed object
    """
    if isinstance(structure, structure_models.Project):
        return list(models.Allocation.objects.filter(is_active=True, project=structure))
    elif isinstance(structure, structure_models.Customer):
        return list(
            models.Allocation.objects.filter(
                is_active=True, project__customer=structure
            )
        )
    else:
        return []


def get_structure_remote_allocations(structure):
    """
    Return all of the allocations associated with the passed object
    """
    if isinstance(structure, structure_models.Project):
        return list(
            models.RemoteAllocation.objects.filter(is_active=True, project=structure)
        )
    elif isinstance(structure, structure_models.Customer):
        return list(
            models.RemoteAllocation.objects.filter(
                is_active=True, project__customer=structure
            )
        )
    else:
        return []


@shared_task(name="waldur_openportal.add_allocated_project")
def add_allocated_project(serialized_allocation):
    """
    Add the allocated project to the OpenPortal backend
    """
    logger.info(f"task.add_allocated_project: {serialized_allocation}")

    if isinstance(serialized_allocation, models.Allocation):
        allocation = serialized_allocation
    else:
        allocation = core_utils.deserialize_instance(serialized_allocation)

        if not isinstance(allocation, models.Allocation):
            logger.info(
                f"Skipping allocation {allocation} - not an openportal.Allocation instance"
            )
            return

    openportal_backend: backend.OpenPortalBackend = allocation.get_backend()
    openportal_backend.add_allocated_project(allocation)


@shared_task(name="waldur_openportal.update_user")
def update_user(serialized_user):
    """
    Update the user by making sure that they are added to all OpenPortal
    resources to which they have allocations.
    """
    logger.info(f"task.update_user: {serialized_user}")

    if isinstance(serialized_user, User):
        user = serialized_user
    else:
        user = core_utils.deserialize_instance(serialized_user)

        if not isinstance(user, User):
            logger.info(f"Skipping user {user} - not a User instance")
            return

    for allocation in utils.get_project_allocations(user):
        try:
            # adding and updating are the same thing in OpenPortal
            backend = allocation.get_backend()

            # This call will make sure to create the project if it
            # failed creation before
            allocation = backend.check_added_allocation(allocation)

            logger.info(f"Adding user {user} to {allocation}")

            backend.add_user(allocation, user)
        except Exception as e:
            logger.error(f"Failed to add {user} to {allocation}: {e}")

    for allocation in utils.get_remote_project_allocations(user):
        try:
            # adding and updating are the same thing in OpenPortal
            backend = allocation.get_backend()

            # This call will make sure to create the project if it
            # failed creation before
            allocation = backend.check_added_allocation(allocation)

            logger.info(f"Adding user {user} to remote {allocation}")

            backend.add_user(allocation, user)
        except Exception as e:
            logger.error(f"Failed to add {user} to remote {allocation}: {e}")


@shared_task(name="waldur_openportal.delete_user")
def delete_user(serialized_user):
    """
    Update the user by deleting them from all OpenPortal resources
    to which they have allocations. This is called when you want to
    completely delete the user.
    """
    logger.info(f"task.delete_user: {serialized_user}")

    if isinstance(serialized_user, User):
        user = serialized_user
    else:
        user = core_utils.deserialize_instance(serialized_user)

        if not isinstance(user, User):
            logger.info(f"Skipping user {user} - not a User instance")
            return

    if not isinstance(user, User):
        logger.error(f"OpenPortal - {user} is not a User instance - it is {type(user)}")
        return

    for allocation in utils.get_project_allocations(user):
        try:
            if not allocation.is_added_to_openportal():
                logger.warning(f"{allocation} not in OpenPortal - skipping")
                continue

            backend = allocation.get_backend()
            allocation = backend.check_added_allocation(allocation)

            logger.info(f"Deleting user {user} from project {allocation}")

            backend.delete_user(allocation, user)
        except Exception as e:
            logger.error(f"Failed to delete {user} from {allocation}: {e}")

    for allocation in utils.get_remote_project_allocations(user):
        try:
            if not allocation.is_added_to_openportal():
                logger.warning(f"{allocation} not in OpenPortal - skipping")
                continue

            backend = allocation.get_backend()
            allocation = backend.check_added_allocation(allocation)

            logger.info(f"Deleting user {user} from remote project {allocation}")

            backend.delete_user(allocation, user)
        except Exception as e:
            logger.error(f"Failed to delete {user} from remote {allocation}: {e}")


@shared_task(name="waldur_openportal.sync_allocation_usage")
def sync_allocation_usage(serialized_allocation):
    """
    This task is called to synchronise the usage for the passed allocation
    """
    logger.info(f"task.sync_allocation_usage: {serialized_allocation}")

    if isinstance(serialized_allocation, models.Allocation):
        allocation = serialized_allocation
    else:
        allocation = core_utils.deserialize_instance(serialized_allocation)

        if not isinstance(allocation, models.Allocation):
            logger.info(
                f"Skipping allocation {allocation} - not an Allocation instance"
            )
            return

    backend = allocation.get_backend()

    allocation = backend.check_added_allocation(allocation)
    backend.sync_usage(allocation)


@shared_task(name="waldur_openportal.sync_remote_allocation_usage")
def sync_remote_allocation_usage(serialized_allocation):
    """
    This task is called to synchronise the usage for the passed allocation
    """
    logger.info(f"task.sync_remote_allocation_usage: {serialized_allocation}")

    if isinstance(serialized_allocation, models.RemoteAllocation):
        allocation = serialized_allocation
    else:
        allocation = core_utils.deserialize_instance(serialized_allocation)

        if not isinstance(allocation, models.RemoteAllocation):
            logger.info(
                f"Skipping allocation {allocation} - not a RemoteAllocation instance"
            )
            return

    backend = allocation.get_backend()

    try:
        allocation = backend.check_added_allocation(allocation)
    except Exception as e:
        if str(e).find("ManagedProjectPendingError") != -1:
            logger.debug(
                f"Allocation {allocation} is still pending in remote portal - skipping usage sync"
            )
        else:
            logger.error(f"Failed to check allocation {allocation}: {e}")

        # just return for now - we can't sync usage as the project is not connected
        return

    backend.sync_usage(allocation)


@shared_task(name="waldur_openportal.sync_remote_allocation_users")
def sync_remote_allocation_users(serialized_allocation):
    """
    This task is called to synchronise the allocations for all users
    associated with all allocations
    """
    logger.info(f"task.sync_remote_allocation_users: {serialized_allocation}")

    if isinstance(serialized_allocation, models.RemoteAllocation):
        allocation = serialized_allocation
    else:
        allocation = core_utils.deserialize_instance(serialized_allocation)

        if not isinstance(allocation, models.RemoteAllocation):
            logger.info(
                f"Skipping allocation {allocation} - not a RemoteAllocation instance"
            )
            return

    backend = allocation.get_backend()

    allocation = backend.check_added_allocation(allocation)

    backend.sync_users(allocation)


@shared_task(name="waldur_openportal.sync_allocation_users")
def sync_allocation_users(serialized_allocation):
    """
    This task is called to synchronise the allocations for all users
    associated with all allocations
    """
    logger.info(f"task.sync_allocation_users: {serialized_allocation}")

    if isinstance(serialized_allocation, models.Allocation):
        allocation = serialized_allocation
    else:
        allocation = core_utils.deserialize_instance(serialized_allocation)

        if not isinstance(allocation, models.Allocation):
            logger.info(
                f"Skipping allocation {allocation} - not an Allocation instance"
            )
            return

    backend = allocation.get_backend()

    allocation = backend.check_added_allocation(allocation)

    backend.sync_users(allocation)


@shared_task(name="waldur_openportal.sync_remote_usage")
@run_once_task(takeover_timeout=60 * 60)
def sync_remote_usage():
    """
    This task is called to synchronise the usage for all remote allocations
    """
    if not openportal_config.ensure_config_loaded():
        logger.debug(
            "OpenPortal not enabled or config not available, skipping sync_remote_usage"
        )
        return

    logger.info("OpenPortal task.sync_remote_usage")
    now = datetime.datetime.now()
    fail_count = 0
    processed_count = 0

    for allocation in list(models.RemoteAllocation.objects.filter(is_active=True)):
        try:
            sync_remote_allocation_usage(allocation)
            processed_count += 1
            if processed_count % 50 == 0:
                logger.info(f"Processed {processed_count} remote allocations")
        except Exception as e:
            logger.error(f"Failed to sync usage for {allocation}: {e}")
            fail_count += 1

            if fail_count > 5 and (datetime.datetime.now() - now).seconds > 60:
                logger.error("Too many failures - aborting")
                return
            elif (datetime.datetime.now() - now).seconds > 3600:
                logger.error("sync_remote_usage took too long - aborting")
                return

    logger.info(f"sync_remote_usage completed: processed {processed_count} allocations")


@shared_task(name="waldur_openportal.sync_customer_allocations")
@run_once_task(takeover_timeout=60 * 60, include_args=True)
def sync_customer_allocations(customer_id):
    """
    This task synchronises the usage for all allocations belonging to a single customer.
    Allocations are processed serially within each customer to avoid race conditions.
    Uses run_once_task with include_args=True to ensure only one instance per customer
    can run at a time, preventing backup of long-running tasks.
    """
    try:
        customer = structure_models.Customer.objects.get(id=customer_id)
    except structure_models.Customer.DoesNotExist:
        logger.error(f"Customer with id {customer_id} does not exist")
        return

    logger.info(
        f"OpenPortal task.sync_customer_allocations for customer {customer.name}"
    )

    # Get all active allocations for this customer
    allocations = models.Allocation.objects.filter(
        is_active=True, project__customer=customer
    )

    for allocation in allocations:
        try:
            sync_allocation_usage(allocation)
        except Exception as e:
            logger.error(f"Failed to sync usage for {allocation}: {e}")


@shared_task(name="waldur_openportal.sync_usage")
@run_once_task(takeover_timeout=60 * 60)
def sync_usage():
    """
    This task is called to synchronise the usage for all allocations.
    It processes allocations by customer in parallel, but serially within each customer.

    Note: This task schedules parallel subtasks and returns immediately.
    The sync_allocation_limits task should be scheduled separately (e.g., via cron)
    to run after this task typically completes to update resource limits.
    """
    if not openportal_config.ensure_config_loaded():
        logger.debug(
            "OpenPortal not enabled or config not available, skipping sync_usage"
        )
        return

    logger.info("OpenPortal task.sync_usage")

    # Group allocations by customer to enable parallel processing
    # Use distinct() to get unique customer IDs
    customer_ids = list(
        models.Allocation.objects.filter(is_active=True)
        .values_list("project__customer_id", flat=True)
        .distinct()
    )

    logger.info(f"OpenPortal task.sync_usage: Processing {len(customer_ids)} customers")

    # Schedule a task for each customer to process their allocations in parallel
    for customer_id in customer_ids:
        try:
            sync_customer_allocations.delay(customer_id)
        except Exception as e:
            logger.error(f"Failed to schedule sync for customer {customer_id}: {e}")


@shared_task(name="waldur_openportal.sync_allocation_storage")
def sync_allocation_storage(serialized_allocation):
    """
    Fetch the current storage snapshot for the passed allocation and merge it
    into the month-accumulated CachedProjectStorageReport.
    """
    logger.info(f"task.sync_allocation_storage: {serialized_allocation}")

    if isinstance(serialized_allocation, models.Allocation):
        allocation = serialized_allocation
    else:
        allocation = core_utils.deserialize_instance(serialized_allocation)

        if not isinstance(allocation, models.Allocation):
            logger.info(
                f"Skipping allocation {allocation} - not an Allocation instance"
            )
            return

    backend_obj = allocation.get_backend()
    backend_obj.sync_storage(allocation)


@shared_task(name="waldur_openportal.sync_storage")
@run_once_task(takeover_timeout=60 * 60)
def sync_storage():
    """
    Fetch and accumulate storage snapshots for all active allocations.
    Runs every 8 hours so each project gets at least one storage report per day
    without hammering the filesystems.
    """
    if not openportal_config.ensure_config_loaded():
        logger.debug(
            "OpenPortal not enabled or config not available, skipping sync_storage"
        )
        return

    logger.info("OpenPortal task.sync_storage")

    allocations = list(models.Allocation.objects.filter(is_active=True))

    # Randomise order so repeated errors on individual allocations don't
    # consistently block others from being processed.
    random.shuffle(allocations)

    for allocation in allocations:
        try:
            sync_allocation_storage(allocation)
        except Exception as e:
            logger.error(f"Failed to sync storage for {allocation}: {e}")


@shared_task(name="waldur_openportal.sync_remote_allocation_storage")
def sync_remote_allocation_storage(serialized_allocation):
    """
    Fetch the accumulated storage report from the remote portal for the
    passed RemoteAllocation and store it in CachedProjectStorageReport.
    """
    logger.info(f"task.sync_remote_allocation_storage: {serialized_allocation}")

    if isinstance(serialized_allocation, models.RemoteAllocation):
        allocation = serialized_allocation
    else:
        allocation = core_utils.deserialize_instance(serialized_allocation)

        if not isinstance(allocation, models.RemoteAllocation):
            logger.info(
                f"Skipping allocation {allocation} - not a RemoteAllocation instance"
            )
            return

    backend_obj = allocation.get_backend()

    try:
        allocation = backend_obj.check_added_allocation(allocation)
    except Exception as e:
        if str(e).find("ManagedProjectPendingError") != -1:
            logger.debug(
                f"Allocation {allocation} is still pending - skipping storage sync"
            )
        else:
            logger.error(f"Failed to check allocation {allocation}: {e}")
        return

    backend_obj.sync_storage(allocation)


@shared_task(name="waldur_openportal.sync_remote_storage")
@run_once_task(takeover_timeout=60 * 60)
def sync_remote_storage():
    """
    Fetch and store accumulated storage reports from remote portals for all
    active RemoteAllocations.
    """
    if not openportal_config.ensure_config_loaded():
        logger.debug(
            "OpenPortal not enabled or config not available, skipping sync_remote_storage"
        )
        return

    logger.info("OpenPortal task.sync_remote_storage")
    now = datetime.datetime.now()
    fail_count = 0

    allocations = list(models.RemoteAllocation.objects.filter(is_active=True))
    random.shuffle(allocations)

    for allocation in allocations:
        try:
            sync_remote_allocation_storage(allocation)
        except Exception as e:
            logger.error(f"Failed to sync storage for {allocation}: {e}")
            fail_count += 1

            if fail_count > 5 and (datetime.datetime.now() - now).seconds > 60:
                logger.error("Too many failures - aborting")
                return
            elif (datetime.datetime.now() - now).seconds > 3600:
                logger.error("sync_remote_storage took too long - aborting")
                return

        if (datetime.datetime.now() - now).seconds > 3600:
            logger.error("sync_remote_storage took too long - aborting")
            return


@shared_task(name="waldur_openportal.sync_local_users")
@run_once_task(takeover_timeout=60 * 60)
def sync_local_users():
    """
    This task runs through all of the allocations and makes sure that all
    users associated with those allocations are properly synced (e.g.
    added or removed)
    """
    if not openportal_config.ensure_config_loaded():
        logger.debug(
            "OpenPortal not enabled or config not available, skipping sync_local_users"
        )
        return

    logger.info("OpenPortal task.sync_local_users")
    now = datetime.datetime.now()

    allocations = list(models.Allocation.objects.filter(is_active=True))

    # randomise the order of the allocations to avoid always processing in the same order and potentially
    # leaving some allocations with unsynced users for a long time
    random.shuffle(allocations)

    for allocation in allocations:
        try:
            sync_allocation_users(allocation)
        except Exception as e:
            logger.error(f"Failed to sync users for {allocation}: {e}")

        if (datetime.datetime.now() - now).seconds > 3600:
            logger.error("sync_users took too long - aborting")
            break


@shared_task(name="waldur_openportal.sync_remote_users")
@run_once_task(takeover_timeout=60 * 60)
def sync_remote_users():
    """
    This task runs through all of the remote allocations and makes sure that all
    users associated with those allocations are properly synced (e.g.
    added or removed)
    """
    if not openportal_config.ensure_config_loaded():
        logger.debug(
            "OpenPortal not enabled or config not available, skipping sync_remote_users"
        )
        return

    logger.info("OpenPortal task.sync_remote_users")
    now = datetime.datetime.now()

    allocations = list(models.RemoteAllocation.objects.filter(is_active=True))

    # randomise the order of the allocations to avoid always processing in the same order and potentially
    # leaving some allocations with unsynced users for a long time
    random.shuffle(allocations)

    for allocation in allocations:
        try:
            sync_remote_allocation_users(allocation)
        except Exception as e:
            logger.error(f"Failed to sync remote users for {allocation}: {e}")

        if (datetime.datetime.now() - now).seconds > 3600:
            logger.error("sync_remote_users took too long - aborting")
            break


@shared_task(name="waldur_openportal.sync_allocation_limits")
@run_once_task(takeover_timeout=60 * 60)
def sync_allocation_limits():
    """
    This task updates the resource limits for all allocations based on project credits
    and current usage. This should be run after sync_usage to ensure all usage data is current.
    """
    if not openportal_config.ensure_config_loaded():
        logger.debug(
            "OpenPortal not enabled or config not available, skipping sync_allocation_limits"
        )
        return

    logger.info("OpenPortal task.sync_allocation_limits")
    now = datetime.datetime.now()
    processed_count = 0

    for project_credit in list(
        invoice_models.ProjectCredit.objects.select_related("project")
    ):
        project = project_credit.project

        # Skip fully removed projects
        if project.is_removed:
            continue

        # For projects in grace period or past grace period, set limits to zero
        if project.is_in_grace_period:
            logger.info(
                f"Project {project} is in grace period (until {project.end_date_with_grace}) - setting limits to zero"
            )
            credits_available = 0
        elif project.is_expired:
            # Project is expired and past grace period
            logger.info(
                f"Project {project} is expired (past grace period) - setting limits to zero"
            )
            credits_available = 0
        else:
            # Project is active, use normal credit logic
            credits_available = project_credit.value

            if credits_available is None or credits_available <= 0:
                credits_available = 0
            else:
                credits_available = float(credits_available)

        # find any openportal allocations associated with the project
        allocations = models.Allocation.objects.filter(project=project, is_active=True)

        if not allocations:
            logger.debug(f"Project {project} has no OpenPortal allocations - skipping")
            continue

        # Calculate the total usage so far this month across OpenPortal allocations
        # for this project - if it exceeds the number of project credits available
        # then we have to set the limits to zero to prevent any more spend
        if credits_available > 0:
            total_spend = 0.0

            for allocation in allocations:
                total_spend += float(allocation.node_usage)

            logger.info(
                f"Total spend for {project} is {total_spend} hours - {credits_available} available"
            )

            if total_spend >= credits_available:
                logger.warning(
                    f"Total spend for {project} exceeds available credits - setting limits to zero"
                )
                credits_available = 0

        for allocation in allocations:
            try:
                if not allocation.is_added_to_openportal():
                    logger.warning(
                        f"Allocation {allocation} not in OpenPortal - skipping"
                    )
                    continue

                if allocation.node_limit is None or allocation.node_limit <= 0:
                    node_limit = 0
                else:
                    node_limit = float(allocation.node_limit)

                backend = allocation.get_backend()

                if abs(node_limit - credits_available) > 0.001:
                    logger.info(
                        f"Setting node limit for {allocation} to {credits_available} hours"
                    )

                    allocation.node_limit = credits_available
                    backend.set_resource_limits(allocation)
                    allocation.save()
                else:
                    # double check that the limit is set correctly
                    current_limit = backend.get_resource_limits(
                        allocation.get_project_identifier()
                    )

                    if (
                        current_limit is None
                        or abs(current_limit.hours - allocation.node_limit) > 0.001
                    ):
                        logger.warning(
                            f"Node limit for {allocation} is not set correctly - changing from {current_limit} to {allocation.node_limit}"
                        )
                        backend.set_resource_limits(allocation)

            except Exception as e:
                logger.error(f"Failed to sync limits for {allocation}: {e}")

            if (datetime.datetime.now() - now).seconds > 3600:
                logger.error("sync_allocation_limits took too long - aborting")
                return

        # Memory cleanup after processing each project
        processed_count += 1
        if processed_count % 100 == 0:
            logger.info(f"Processed {processed_count} project credits")

    logger.info(
        f"sync_allocation_limits completed: processed {processed_count} credits"
    )


@shared_task(name="waldur_openportal.sync_remote")
@run_once_task(takeover_timeout=60 * 60)
def sync_remote():
    """
    This is a full OpenPortal remote sync - this will go through all remote projects
    and make sure that they have been created and any updates applied
    """
    logger.info("OpenPortal task.sync_remote")
    now = datetime.datetime.now()
    fail_count = 0

    # First, try to create all of the remote projects that are not
    # already created in the remote portal
    for remote_allocation in models.RemoteAllocation.objects.filter(is_active=True):
        project = remote_allocation.project

        if project is None:
            logger.warning(
                f"Remote allocation {remote_allocation} has no associated project - deleting"
            )
            try:
                remote_allocation.delete()
            except Exception as e:
                logger.error(
                    f"Failed to delete remote allocation {remote_allocation}: {e}"
                )
            continue

        # Skip removed projects or projects past grace period
        if project.is_removed:
            logger.info(
                f"Remote allocation {remote_allocation} is for a removed project - deleting"
            )
            try:
                remote_allocation.delete()
            except Exception as e:
                logger.error(
                    f"Failed to delete remote allocation {remote_allocation}: {e}"
                )
            continue

        # Delete allocations for projects past grace period (fully expired)
        if project.is_expired and not project.is_in_grace_period:
            logger.info(
                f"Remote allocation {remote_allocation} is for a project past grace period - deleting"
            )
            try:
                remote_allocation.delete()
            except Exception as e:
                logger.error(
                    f"Failed to delete remote allocation {remote_allocation}: {e}"
                )
            continue

        # Projects in grace period are kept but will have limits set to zero by sync_usage

        if remote_allocation.state not in [
            CoreStates.CREATION_SCHEDULED,
            CoreStates.CREATING,
            CoreStates.UPDATE_SCHEDULED,
            CoreStates.UPDATING,
            CoreStates.OK,
        ]:
            logger.debug(
                f"Remote allocation {remote_allocation} is not in a valid state {remote_allocation.state} for syncing - skipping"
            )
            continue

        try:
            backend = remote_allocation.get_backend()

            if not remote_allocation.is_added_to_openportal():
                logger.info(
                    f"Remote allocation {remote_allocation} not in OpenPortal - adding"
                )
                backend.add_allocated_project(remote_allocation)
            elif remote_allocation.needs_updating():
                logger.info(
                    f"Remote allocation {remote_allocation} needs updating ({remote_allocation.local_version} vs {remote_allocation.remote_version}) - updating"
                )
                backend.update_allocated_project(remote_allocation, force_update=False)

        except Exception as e:
            logger.error(f"Failed to sync remote project {remote_allocation}: {e}")
            fail_count += 1

            if fail_count > 5 and (datetime.datetime.now() - now).seconds > 60:
                logger.error("Too many failures - aborting")
                break
            elif (datetime.datetime.now() - now).seconds > 3600:
                logger.error("sync_remote_usage took too long - aborting")
                break


@shared_task(name="waldur_openportal.sync")
@run_once_task(takeover_timeout=60 * 60)
def sync():
    """
    Perform a complete sync of OpenPortal.
    This will add and remove users as needed.
    """
    logger.info("OpenPortal task.sync")


@shared_task(name="waldur_openportal.sync_project")
def sync_project(serialized_project):
    """
    This is a project sync - this will go through all users associated
    with the project and ensure that they have the correct associations
    with any OpenPortal allocations. This will add and remove users as needed.
    """
    logger.info(f"OpenPortal task.sync_project: {serialized_project}")

    if isinstance(serialized_project, structure_models.Project):
        project = serialized_project
    else:
        project = core_utils.deserialize_instance(serialized_project)

        if not isinstance(project, structure_models.Project):
            logger.info(f"Skipping project {project} - not a Project instance")
            return

    now = datetime.datetime.now()
    fail_count = 0

    for allocation in get_structure_allocations(project):
        try:
            sync_allocation_users(allocation)
        except Exception as e:
            logger.error(f"Failed to sync users for {allocation}: {e}")
            fail_count += 1

            if fail_count > 5 and (datetime.datetime.now() - now).seconds > 60:
                logger.error("Too many failures - aborting")
                break
            elif (datetime.datetime.now() - now).seconds > 3600:
                logger.error("sync_project took too long - aborting")
                break

    for allocation in get_structure_remote_allocations(project):
        try:
            sync_remote_allocation_users(allocation)
        except Exception as e:
            logger.error(f"Failed to sync remote users for {allocation}: {e}")
            fail_count += 1

            if fail_count > 5 and (datetime.datetime.now() - now).seconds > 60:
                logger.error("Too many failures - aborting")
                break
            elif (datetime.datetime.now() - now).seconds > 3600:
                logger.error("sync_remote_usage took too long - aborting")
                break


@shared_task(name="waldur_openportal.send_notifications")
def send_notifications():
    """
    This task is called to send notifications to all users associated
    with any OpenPortal allocations.
    """
    logger.info("OpenPortal task.send_notifications")

    # make sure that we only run during "office hours"
    # (10am to 3pm) - this is a bit of a hack, but it will do for now
    now = datetime.datetime.now()

    if now.hour < 10 or now.hour > 15:
        logger.debug("Not sending notifications - outside office hours")
        return

    # Get today's date
    today = datetime.date.today()

    num_emails_sent = 0

    # Loop over all ProjectCredit objects - no need to send notifications
    # to projects that don't have any credits allocated
    for project_credit in invoice_models.ProjectCredit.objects.all():
        if num_emails_sent > 500:
            logger.warning("Sent over 500 emails already - stopping")
            return

        project = project_credit.project

        # Skip removed projects
        if project.is_removed:
            continue

        # Skip projects that are expired (including grace period)
        # We don't send notifications for expired projects
        if project.is_expired:
            continue

        # get the end date for this project
        end_date = project.end_date

        # Double-check that the project is not expired
        if end_date is not None and end_date < today:
            # project is expired - no need to send notifications
            continue

        # Check to see if this project should be notified
        notification, created = models.ProjectNotification.objects.get_or_create(
            project=project
        )

        if notification.frequency == 0:
            # No notifications - skip
            continue

        should_notify = False

        if created or notification.last_notification is None:
            should_notify = True
        elif (
            notification.last_notification
            + datetime.timedelta(days=notification.frequency)
            <= today
        ):
            should_notify = True

        if not should_notify:
            continue

        # Check to see if this project has any credits available
        credits_available = float(project_credit.value)

        if credits_available is None or credits_available <= 0:
            credits_available = 0
        else:
            credits_available = float(credits_available)

        # find any openportal allocations associated with the project
        # (note we don't do this for RemoteAllocations, as the remote
        #  portal should be handling this)
        allocations = models.Allocation.objects.filter(project=project, is_active=True)

        # Skip projects with no allocations
        if not allocations:
            logger.debug(f"Project {project} has no OpenPortal allocations - skipping")
            continue

        # Calculate the total usage so far this month across OpenPortal allocations
        total_spend = 0.0

        for allocation in allocations:
            total_spend += float(allocation.node_usage)

        notification_subject = _notification_subject(project, today)

        notification_body = _notification_body(
            project,
            today,
            credits_available,
            total_spend,
            end_date,
            notification.frequency,
        )

        # Send the notification to each user - wait 50ms between each
        # notification to avoid overwhelming the mail server
        for user in project.get_users():
            try:
                logger.info(f"Sending notification to {user} in {project}")
                logger.debug(f"Notification subject: {notification_subject}")
                logger.debug(f"Notification body: {notification_body}")

                core_utils.send_mail(
                    subject=notification_subject,
                    body=notification_body,
                    to=[user.email],
                )
                num_emails_sent += 1
                time.sleep(0.05)
            except Exception as e:
                logger.error(f"Failed to send notification to {user.email}: {e}")

        # Update the last notification date
        notification.last_notification = today
        notification.save()


def _notification_subject(project, today):
    """
    This function returns the subject for the notification email
    for the passed project generated on the passed date
    """
    return f"Isambard Project Status Update - {today.strftime('%d %B %Y')}"


def _notification_body(
    project, today, credits_available, total_spend, end_date, update_frequency
):
    """
    This function returns the body for the notification email
    for the passed project generated on the passed date. This
    communicates the number of credits available, the total spend
    on the project, and when the project will end.
    """

    remaining = credits_available - total_spend
    if remaining < 0:
        remaining = 0

    if end_date is None:
        date_info = ""
    else:
        date = end_date.strftime("%d %B %Y")

        days_remaining = (end_date - today).days

        if days_remaining < 0:
            days_remaining = "today"
        elif days_remaining == 1:
            days_remaining = "tomorrow"
        else:
            days_remaining = f"in {days_remaining} days time"

        date_info = f"""

All node hours must be consumed before the {date}, which is {days_remaining}.

You must copy back all data before this date. You won't be able to login after your project ends and all remaining data will be deleted."""

    if update_frequency < 1:
        update_frequency = 1

    if update_frequency == 1:
        update_frequency = "day"
    elif update_frequency == 7:
        update_frequency = "week"
    elif update_frequency == 14:
        update_frequency = "fortnight"
    else:
        update_frequency = f"{update_frequency} days"

    # This would eventually be better templated ;-)
    body = f"""
Here is your regular update for your Isambard project “{project.name}”

To date, {total_spend:.2f} node hours have been used, leaving {remaining:.2f} remaining to consume before the end of your project.{date_info}

For more detail, view your project at https://portal.isambard.ac.uk.

To learn more about project accounting, read the documentation at https://docs.isambard.ac.uk/user-documentation/guides/accounting.

If you have any queries, please raise a ticket at https://support.isambard.ac.uk.

We will send you an update every {update_frequency}.

If you want to change the frequency of these updates, please ask the project PI to raise a request at https://support.isambard.ac.uk.

"""

    return body


@shared_task(name="waldur_openportal.update_remote_project")
def update_remote_project(serialized_project):
    """
    This task will look for any remote projects attached to the passed project,
    and will send remote update commands so that the updates are also
    reflected in the remote portal.
    """
    logger.info(f"OpenPortal task.update_remote_project: {serialized_project}")

    if isinstance(serialized_project, structure_models.Project):
        project = serialized_project
    else:
        project = core_utils.deserialize_instance(serialized_project)

        if not isinstance(project, structure_models.Project):
            logger.info(f"Skipping project {project} - not a Project instance")
            return

    # find the remote allocations for this project
    remote_allocations = models.RemoteAllocation.objects.filter(
        project=project, is_active=True
    )

    for remote_allocation in remote_allocations:
        try:
            backend = remote_allocation.get_backend()

            logger.info(f"Updating remote project {remote_allocation}")

            backend.update_allocated_project(remote_allocation)
        except Exception as e:
            logger.error(f"Failed to update remote project {remote_allocation}: {e}")


@shared_task(name="waldur_openportal.delete_remote_project")
def delete_remote_project(serialized_project):
    """
    This task will look for any remote projects attached to the passed project,
    and will send remote delete commands so that the updates are also
    reflected in the remote portal.
    """
    logger.info(f"OpenPortal task.delete_remote_project: {serialized_project}")

    if isinstance(serialized_project, structure_models.Project):
        project = serialized_project
    else:
        project = core_utils.deserialize_instance(serialized_project)

        if not isinstance(project, structure_models.Project):
            logger.info(f"Skipping project {project} - not a Project instance")
            return

    # find the remote allocations for this project
    remote_allocations = models.RemoteAllocation.objects.filter(
        project=project, is_active=True
    )

    for remote_allocation in remote_allocations:
        try:
            backend = remote_allocation.get_backend()

            logger.info(f"Deleting remote project {remote_allocation}")

            backend.delete_allocation(remote_allocation)
        except Exception as e:
            logger.error(f"Failed to delete remote project {remote_allocation}: {e}")


@shared_task(name="waldur_openportal.create_default_resources")
def create_default_resources(serialized_managed_project):
    """
    This task is called to create the default resources for a managed project.
    It will create the default resources in the OpenPortal board.
    """
    logger.info(
        f"OpenPortal task.create_default_resources: {serialized_managed_project}"
    )

    if isinstance(serialized_managed_project, models.ManagedProject):
        managed_project = serialized_managed_project
    else:
        managed_project = core_utils.deserialize_instance(serialized_managed_project)

    if not isinstance(managed_project, models.ManagedProject):
        logger.error(
            f"OpenPortal - {managed_project} is not a ManagedProject instance - it is {type(managed_project)}"
        )
        raise ValueError(
            f"OpenPortal - {managed_project} is not a ManagedProject instance - it is {type(managed_project)}"
        )

    project = managed_project.project

    if project is None:
        logger.error(
            f"OpenPortal - ManagedProject {managed_project} has no associated project"
        )
        raise ValueError(
            f"OpenPortal - ManagedProject {managed_project} has no associated project"
        )

    if project.is_removed:
        logger.info(
            f"OpenPortal - ManagedProject {managed_project} is for a removed project"
        )
        raise ValueError(
            f"OpenPortal - ManagedProject {managed_project} is for a removed project"
        )

    # Prevent creating resources for projects past grace period
    if project.is_expired and not project.is_in_grace_period:
        logger.info(
            f"OpenPortal - ManagedProject {managed_project} is for a project past grace period"
        )
        raise ValueError(
            f"OpenPortal - ManagedProject {managed_project} is for a project past grace period"
        )

    # Projects in grace period can keep their resources but with zero limits

    offerings = managed_project.get_default_offerings()

    logger.info(f"OpenPortal - Creating default resources for {project} - {offerings}")

    for offering in offerings:
        # find any existing orders for this project and offering
        num_erred = 0
        have_existing = False
        resource = None

        for existing_resource in marketplace_models.Resource.objects.filter(
            project=project,
            offering=offering,
        ):
            if existing_resource.state != marketplace_models.Resource.States.ERRED:
                # There is an existing resource in a non-error state. This indicates
                # that the resource is either running, or has been removed in the
                # remote portal. DO NOT RECREATE IT.
                have_existing = True
                logger.info(
                    f"OpenPortal - Found existing resource {existing_resource} for {offering} in {project}"
                )
                resource = existing_resource
                break
            else:
                num_erred += 1

                if num_erred > 5:
                    # This indicates we've tried many times to create this resource
                    # and have failed. We should not try to create it again.
                    logger.error(
                        f"OpenPortal - Too many resources in ERRED state for {offering} in {project} - skipping"
                    )
                    have_existing = True
                    break

        if have_existing:
            logger.info(
                f"OpenPortal - Skipping creation of {offering} for {project} - already exists"
            )

            if resource is not None:
                # check that we haven't created too many plans for this resource
                plan_period = marketplace_models.ResourcePlanPeriod.objects.filter(
                    resource=resource, end=None
                )

                if len(plan_period) > 1:
                    # This indicates we've created multiple plans for the same resource
                    # and project. We should not try to create it again.
                    logger.error(
                        f"OpenPortal - Too many active plans for {resource} in {project} - deleting extras"
                    )

                    for extra_plan in plan_period[1:]:
                        logger.error(f"OpenPortal - Deleting extra plan {extra_plan}")
                        extra_plan.delete()

            continue

        logger.info(f"OpenPortal - Creating {offering} for {project}")

        created_resource = False

        try:
            # Create the resource for the project and offering
            # Look at OrderCreateSerializer.create for how this is done
            # by the marketplace backend
            resource = marketplace_models.Resource.objects.create(
                name=str(offering.name),
                project=project,
                offering=offering,
            )
            resource.init_cost()
            resource.save()

            # Create the order for the project and offering
            # Simply creating the order will trigger the background
            # tasks needed to create the resource.
            # We don't need to do anything more
            marketplace_models.Order.objects.create(
                project=project,
                offering=offering,
                resource=resource,
                created_by=utils.get_openportal_robot(),
                consumer_reviewed_by=utils.get_openportal_robot(),
                plan=offering.plans.first(),
                attributes={
                    "name": str(offering.name),
                    "description": "Default resource created at the start of the project",
                },
            )

            created_resource = True

            # make sure we don't have too many plans for this resource
            plan_period = marketplace_models.ResourcePlanPeriod.objects.filter(
                resource=resource, end=None
            )

            if len(plan_period) > 1:
                # This indicates we've created multiple plans for the same resource
                # and project. We should not try to create it again.
                logger.error(
                    f"OpenPortal - Too many active plans for {resource} in {project} - deleting extras"
                )

                for extra_plan in plan_period[1:]:
                    logger.error(f"OpenPortal - Deleting extra plan {extra_plan}")
                    extra_plan.delete()

        except Exception as e:
            logger.error(
                f"OpenPortal - Failed to create order for {offering} in {project}: {e}"
            )

        if created_resource:
            num_successful = 0

            # make sure that we only have one copy of this resource in the project
            for existing_resource in marketplace_models.Resource.objects.filter(
                project=project,
                offering=offering,
            ):
                if existing_resource.state == marketplace_models.Resource.States.ERRED:
                    # remove previously failed resource creation attempts
                    logger.info(
                        f"OpenPortal - Removing previously failed resource {existing_resource} for {offering} in {project}"
                    )
                    existing_resource.delete()
                elif existing_resource.state == marketplace_models.Resource.States.OK:
                    num_successful += 1

                    if num_successful > 1:
                        # This indicates we've created multiple resources for the same offering
                        # and project. We should not try to create it again.
                        logger.error(
                            f"OpenPortal - Too many resources created for {offering} in {project} - deleting {existing_resource}"
                        )
                        existing_resource.delete()


def update_project(
    board: OpenPortalBoard,
    project: openportal.ProjectIdentifier,
    details: openportal.AwardDetails,
    force_approve: bool = False,
) -> openportal.ProjectMapping:
    """
    Update the project in the OpenPortal board with the given details.
    If the project does not exist, then there will be an error.
    """
    mapping = board.update_project(project, details, force_approve=force_approve)

    # schedule creation of default resources again in case any were missed
    try:
        managed_projects = models.ManagedProject.objects.filter(
            identifier=str(mapping.project),
            destination=str(board.destination()),
        )

        for managed_project in managed_projects:
            if managed_project.is_approved():
                create_default_resources.delay(
                    core_utils.serialize_instance(managed_project)
                )
    except Exception as e:
        logger.error(f"Failed to find managed project for {mapping.project}: {e}")
        raise ValueError(f"Failed to find managed project for {mapping.project}")

    return mapping


def create_project(
    board: OpenPortalBoard,
    identifier: openportal.ProjectIdentifier,
    details: openportal.AwardDetails,
) -> openportal.ProjectMapping:
    """
    Create a project in the OpenPortal board with the given identifier and details.
    """
    # first, create the project if it doesn't exist
    mapping = board.create_project(identifier, details)

    # next, update the details of the project to match the details provided.
    # This will also create the default resources for the project
    mapping = update_project(board, mapping.project, details)

    return mapping


@shared_task(name="waldur_openportal.managed_project_approved")
def managed_project_approved(serialized_managed_project):
    """
    This task is called when a managed project is approved. It will create the
    default resources for the project.
    """
    logger.info(
        f"OpenPortal task.managed_project_approved: {serialized_managed_project}"
    )

    if isinstance(serialized_managed_project, models.ManagedProject):
        managed_project = serialized_managed_project
    else:
        managed_project = core_utils.deserialize_instance(serialized_managed_project)

    if not isinstance(managed_project, models.ManagedProject):
        logger.error(
            f"OpenPortal - {managed_project} is not a ManagedProject instance - it is {type(managed_project)}"
        )
        raise ValueError(
            f"OpenPortal - {managed_project} is not a ManagedProject instance - it is {type(managed_project)}"
        )

    if not managed_project.is_approved:
        logger.error(
            f"OpenPortal - ManagedProject {managed_project} is not approved - cannot call handler!"
        )
        raise ValueError(
            f"OpenPortal - ManagedProject {managed_project} is not approved - cannot call handler!"
        )

    board = OpenPortalBoard(managed_project.get_destination())
    identifier = managed_project.get_remote_identifier()
    details = managed_project.get_details()

    result = update_project(board, identifier, details, force_approve=True)

    logger.info(
        f"OpenPortal - Managed project {managed_project} approved - mapping is {result}"
    )


@shared_task(name="waldur_openportal.run_job")
def run_job(serialized_job):
    """
    This task is called to run a job that has been pulled from the
    OpenPortal jobs board. It will deserialize the job and then
    call the backend to run it.
    """
    logger.info(f"OpenPortal task.run_job: {serialized_job}")

    if isinstance(serialized_job, models.Job):
        job = serialized_job
    else:
        job = core_utils.deserialize_instance(serialized_job)

    if not isinstance(job, models.Job):
        logger.error(f"OpenPortal - {job} is not a Job instance - it is {type(job)}")
        return

    job_model = job

    if job_model.state != models.Job.State.PENDING:
        logger.info(f"OpenPortal - Job {job.job_id} is not pending - skipping")
        return

    try:
        job = job.get_job()
    except Exception as e:
        logger.error(f"OpenPortal - Failed to get job {job.id}: {e}")
        return

    if not job:
        logger.error(f"OpenPortal - Job {job.id} not found")
        return

    if job.state != openportal.Status.pending():
        logger.info(f"OpenPortal - Job {job.id} is not pending - skipping")
        return

    job_model.state = models.Job.State.RUNNING
    job_model.save()

    board = OpenPortalBoard(job.destination)

    logger.info(f"Running job {job} - status {job.state}")

    command = job.instruction.command
    args = job.instruction.arguments

    try:
        result = None

        if command == "create_project":
            identifier = openportal.ProjectIdentifier(args[0])
            details = openportal.AwardDetails(args[1])
            result = create_project(board, identifier, details)
        elif command == "remove_project":
            identifier = openportal.ProjectIdentifier(args[0])
            result = board.remove_project(identifier)
        elif command == "update_project":
            identifier = openportal.ProjectIdentifier(args[0])
            details = openportal.AwardDetails(args[1])
            result = update_project(board, identifier, details)
        elif command == "get_project":
            identifier = openportal.ProjectIdentifier(args[0])
            result = board.get_project(identifier)
        elif command == "get_projects":
            identifier = openportal.PortalIdentifier(args[0])
            result = board.get_projects(identifier)
        elif command == "get_project_mapping":
            identifier = openportal.ProjectIdentifier(args[0])
            result = board.get_project_mapping(identifier)
        elif command == "get_usage_report":
            identifier = openportal.ProjectIdentifier(args[0])
            if len(args) > 1:
                dates = openportal.DateRange.parse(args[1])
            else:
                dates = openportal.DateRange.this_month()
            result = board.get_usage_report(identifier, dates)
        elif command == "get_usage_reports":
            identifier = openportal.PortalIdentifier(args[0])
            if len(args) > 1:
                dates = openportal.DateRange.parse(args[1])
            else:
                dates = openportal.DateRange.this_month()
            result = board.get_usage_reports(identifier, dates)
        elif command == "get_storage_report":
            identifier = openportal.ProjectIdentifier(args[0])
            if len(args) > 1:
                dates = openportal.DateRange.parse(args[1])
            else:
                dates = openportal.DateRange.this_month()
            result = board.get_storage_report(identifier, dates)
        elif command == "get_storage_reports":
            identifier = openportal.PortalIdentifier(args[0])
            if len(args) > 1:
                dates = openportal.DateRange.parse(args[1])
            else:
                dates = openportal.DateRange.this_month()
            result = board.get_storage_reports(identifier, dates)
        else:
            raise ValueError(f"Unknown command {command} for job {job.id}")

        job = job.completed(result)

        # save the job data back to the model so that we don't repeat this job
        job_model.state = models.Job.State.COMPLETED
        job_model.job_data = job.to_json()
        job_model.save()

        result_sent = False
        num_attempts = 0

        while not result_sent and num_attempts < 5:
            try:
                num_attempts += 1
                board.send_result(job)
                result_sent = True
            except Exception as e:
                logger.error(
                    f"OpenPortal - Failed to send result for job {job.id}: {e} - retrying..."
                )
                # celery sleep
                time.sleep(1)

        if not result_sent:
            logger.error(
                f"OpenPortal - Failed to send result for job {job.id} after {num_attempts} attempts"
            )

    except Exception as e:
        logger.error(f"OpenPortal - Failed to run job {job.id}: {e}")
        try:
            job = job.errored(str(e))
        except Exception as e:
            logger.error(
                f"OpenPortal - Failed to set error result for job {job.id}: {e}"
            )

        # save the job model back to the database
        job_model.state = models.Job.State.COMPLETED
        job_model.job_data = job.to_json()
        job_model.save()

        result_sent = False
        num_attempts = 0

        while not result_sent and num_attempts < 5:
            try:
                num_attempts += 1
                board.send_result(job)
                result_sent = True
            except Exception as e:
                logger.error(
                    f"OpenPortal - Failed to send result for job {job.id}: {e} - retrying..."
                )
                time.sleep(1)

        if not result_sent:
            logger.error(
                f"OpenPortal - Failed to send result for job {job.id} after {num_attempts} attempts"
            )


@shared_task(name="waldur_openportal.sync_offering_agents")
def sync_offering_agents():
    """
    This task is called to sync the agents for all offerings
    that are associated with remote OpenPortal backends.
    """
    if not openportal_config.ensure_config_loaded():
        logger.info(
            "OpenPortal not enabled or config not available, skipping sync_offering_agents"
        )
        return

    logger.info("OpenPortal task.sync_offering_agents")

    # get the name of this portal
    portal = openportal.get_portal()

    offerings = []

    # get all of the ProjectTemplate objects
    for template in models.ProjectTemplate.objects.all():
        try:
            offering = openportal.Destination(
                f"{template.get_offering()}.{portal}.{template.get_portal()}"
            )
        except Exception as e:
            logger.error(f"Failed to get offering for template {template}: {e}")
            continue

        offerings.append(offering)

    # now run the jobs to sync all the agent offerings
    openportal.sync_offerings(offerings)


@shared_task(name="waldur_openportal.sync_board")
def sync_board():
    """
    This task is called to synchronise the board to check if OpenPortal
    has received any jobs. If it has, then it pulls the job from the
    board and then spawns a new task to process the job.
    """
    if not openportal_config.ensure_config_loaded():
        logger.info(
            "OpenPortal not enabled or config not available, skipping sync_board"
        )
        return

    jobs = openportal.fetch_jobs()

    if len(jobs) == 0:
        return

    for job in jobs:
        try:
            if job.state != openportal.Status.PENDING:
                logger.info(f"Job {job.id} is not pending - skipping")
                continue

            logger.info(f"Processing job {job} from OpenPortal board")
            j = models.Job.objects.create(
                id=str(job.id),
                data=job.to_json(),
            )

            run_job.delay(core_utils.serialize_instance(j))

        except Exception as e:
            logger.error(f"Failed to process job {job.id}: {e}")
            continue


@shared_task(name="waldur_openportal.clean_stale_jobs")
def clean_stale_jobs():
    """
    This task deletes all OpenPortal jobs that were created more than
    2 days ago - this is to prevent the database from filling up with
    old jobs that are no longer relevant.
    """
    logger.info("OpenPortal task.clean_stale_jobs")

    cutoff = datetime.date.today() - datetime.timedelta(days=2)

    stale_jobs = models.Job.objects.filter(created__lt=cutoff)

    stale_jobs.delete()


@shared_task(name="waldur_openportal.fix_total_allocation")
def fix_total_allocation():
    """
    This task goes through all OpenPortal remote allocations and makes sure
    that the project balance is correct, given the total awarded from
    the remote allocation, and the total consumption for the project
    over its lifetime. We have seen that these can drift apart
    over time, as Waldur does some strange accounting at the start
    of each month. This should be run daily
    """
    logger.info("OpenPortal task.fix_total_allocation")

    managed_projects = models.ManagedProject.objects.filter(project__isnull=False)

    for managed_project in managed_projects:
        project = managed_project.project

        if project.is_removed:
            continue

        if project.is_expired:
            continue

        utils.fix_total_allocation(project)


@shared_task(name="waldur_openportal.notify_users_about_rejected_allocation")
def notify_users_about_rejected_allocation(serialized_managed_project):
    """
    Send a rejection notification to the admins and managers of the
    Waldur project linked to the managed project, when its resource
    allocation request has been rejected.
    """
    logger.info(
        "OpenPortal task.notify_users_about_rejected_allocation: %s",
        serialized_managed_project,
    )

    managed_project = core_utils.deserialize_instance(serialized_managed_project)

    if not isinstance(managed_project, models.ManagedProject):
        logger.error(
            "OpenPortal - %s is not a ManagedProject instance - it is %s",
            managed_project,
            type(managed_project),
        )
        raise ValueError(
            f"OpenPortal - {managed_project} is not a ManagedProject instance - it is {type(managed_project)}"
        )

    if not managed_project.is_rejected():
        logger.error(
            "OpenPortal - ManagedProject %s is not rejected - cannot send rejection notification!",
            managed_project,
        )
        raise ValueError(
            f"OpenPortal - ManagedProject {managed_project} is not rejected - cannot send rejection notification!"
        )

    project = managed_project.project
    if project is None:
        logger.warning(
            "OpenPortal - ManagedProject %s has no linked Waldur project - skipping rejection notification",
            managed_project,
        )
        return

    reviewer = managed_project.reviewed_by
    if reviewer is None:
        logger.warning(
            "OpenPortal - ManagedProject %s has no reviewer - skipping rejection notification",
            managed_project,
        )
        return

    admins = get_users(project, RoleEnum.PROJECT_ADMIN)
    managers = get_users(project, RoleEnum.PROJECT_MANAGER)
    recipients = {u.id: u for u in [*admins, *managers] if u.email}

    if not recipients:
        logger.warning(
            "OpenPortal - project %s has no admins or managers with an email - skipping rejection notification",
            project,
        )
        return

    details = managed_project.get_details()
    project_name = details.name or managed_project.identifier

    for user in recipients.values():
        context = {
            "recipient_first_name": user.first_name,
            "project_name": project_name,
            "reviewer_full_name": reviewer.full_name,
            "reviewer_email": reviewer.email,
            "reviewer_organization": reviewer.organization,
            "review_comment": managed_project.review_comment or "",
            "site_name": config.SITE_NAME,
        }
        logger.info(
            "OpenPortal - sending rejection notification to %s for project %s",
            user.email,
            managed_project,
        )
        core_utils.broadcast_mail(
            "openportal",
            "managed_project_rejected",
            context,
            [user.email],
        )
