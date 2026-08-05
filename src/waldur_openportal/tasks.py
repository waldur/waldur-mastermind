import datetime
import functools
import json
import logging
import random
import time

import openportal
from celery import shared_task
from constance import config as constance_config

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import User
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import backend, config, models, remote_project_service, utils
from .board import OpenPortalBoard, _trim_job

logger = logging.getLogger(__name__)


def run_once_task(takeover_timeout, include_args=False):
    """
    Decorator to ensure only one instance of a task runs at a time.

    The lock is a database row that is deleted in a ``finally``, so it only
    outlives the task if the process dies without unwinding - a hard kill or an
    OOM. ``takeover_timeout`` is the sole recovery path from that state.

    ``last_run`` records when the task *started* and is deliberately never
    refreshed while it runs: there is no heartbeat. That is safe only because
    Celery kills any task well before the lock could expire under it. Keep
    ``takeover_timeout`` generously above ``CELERY_TASK_TIME_LIMIT`` (currently
    30 minutes) to account for queue wait and scheduling delays, the same
    reasoning ``BackgroundTask.lock_timeout`` follows in waldur_core. Lowering it
    below that limit would let a still-running task have its lock taken over and
    end up running twice.

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
                    # takeover_timeout seconds ago?  total_seconds() rather than
                    # seconds: the latter is the seconds-within-the-day part, so
                    # a lock orphaned for 24h reads as 0 elapsed and is never
                    # taken over.  Both values are UTC-aware (USE_TZ is on), so
                    # they subtract directly.
                    if (
                        lock.last_run is None
                        or (now - lock.last_run).total_seconds() > takeover_timeout
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
                            logger.debug(
                                f"OpenPortal task {lock_id} already running - skipping"
                            )
                            return False
                    else:
                        logger.debug(
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
                    return func(*args, **kwargs)
                finally:
                    release_lock()

            # Lock held elsewhere - indistinguishable from a task that returned
            # None, which is fine for these fire-and-forget beat tasks.
            return None

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
            logger.debug(
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
            logger.debug(f"Skipping user {user} - not a User instance")
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
            logger.debug(f"Skipping user {user} - not a User instance")
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
            logger.debug(
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
            logger.debug(
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
            logger.debug(
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
            logger.debug(
                f"Skipping allocation {allocation} - not an Allocation instance"
            )
            return

    backend = allocation.get_backend()

    allocation = backend.check_added_allocation(allocation)

    backend.sync_users(allocation)


@shared_task(name="waldur_openportal.sync_remote_usage")
def sync_remote_usage():
    """
    Dispatcher: fans out one sync_remote_usage_for_destination subtask per active
    destination so that a down destination cannot block usage syncs for others.
    """
    if not config.ensure_config_loaded():
        logger.debug(
            "OpenPortal not enabled or config not available, skipping sync_remote_usage"
        )
        return

    logger.info("OpenPortal task.sync_remote_usage")

    service_settings_ids = list(
        models.RemoteAllocation.objects.filter(is_active=True)
        .values_list("service_settings_id", flat=True)
        .distinct()
    )

    logger.info(
        f"OpenPortal task.sync_remote_usage: dispatching sync for {len(service_settings_ids)} destination(s)"
    )

    for sid in service_settings_ids:
        try:
            sync_remote_usage_for_destination.delay(sid)
        except Exception as e:
            logger.error(
                f"Failed to dispatch sync_remote_usage_for_destination for service_settings {sid}: {e}"
            )


@shared_task(name="waldur_openportal.sync_remote_usage_for_destination")
@run_once_task(takeover_timeout=60 * 60, include_args=True)
def sync_remote_usage_for_destination(service_settings_id):
    """
    Sync usage for all RemoteAllocations belonging to a single destination.
    """
    try:
        service_settings = structure_models.ServiceSettings.objects.get(
            pk=service_settings_id
        )
    except structure_models.ServiceSettings.DoesNotExist:
        logger.error(
            f"sync_remote_usage_for_destination: ServiceSettings {service_settings_id} does not exist"
        )
        return

    logger.info(
        f"OpenPortal task.sync_remote_usage_for_destination: {service_settings}"
    )

    allocations = list(
        models.RemoteAllocation.objects.filter(
            is_active=True, service_settings=service_settings
        )
    )
    random.shuffle(allocations)

    now = datetime.datetime.now()
    fail_count = 0

    for allocation in allocations:
        if (datetime.datetime.now() - now).seconds > 3600:
            logger.error(
                f"sync_remote_usage_for_destination: {service_settings} took too long - aborting"
            )
            return

        try:
            sync_remote_allocation_usage(allocation)
        except Exception as e:
            logger.error(f"Failed to sync usage for {allocation}: {e}")
            fail_count += 1

            if fail_count > 25 and (datetime.datetime.now() - now).seconds > 600:
                logger.error(
                    f"sync_remote_usage_for_destination: {service_settings} - too many failures, aborting"
                )
                return


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
    allocations = list(
        models.Allocation.objects.filter(is_active=True, project__customer=customer)
    )

    # randomise the order of the allocations to avoid always processing in the same order and potentially
    # leaving some allocations with outdated usage for a long time
    random.shuffle(allocations)

    now = datetime.datetime.now()

    for allocation in allocations:
        try:
            sync_allocation_usage(allocation)
        except Exception as e:
            logger.error(f"Failed to sync usage for {allocation}: {e}")

        if (datetime.datetime.now() - now).seconds > 3600:
            logger.error("sync_customer_allocations took too long - aborting")
            return


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
    if not config.ensure_config_loaded():
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
            logger.debug(
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
    if not config.ensure_config_loaded():
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
            logger.debug(
                f"Skipping allocation {allocation} - not a RemoteAllocation instance"
            )
            return

    backend = allocation.get_backend()

    try:
        allocation = backend.check_added_allocation(allocation)
    except Exception as e:
        if str(e).find("ManagedProjectPendingError") != -1:
            logger.debug(
                f"Allocation {allocation} is still pending - skipping storage sync"
            )
        else:
            logger.error(f"Failed to check allocation {allocation}: {e}")
        return

    backend.sync_storage(allocation)


@shared_task(name="waldur_openportal.refresh_remote_projects")
@run_once_task(takeover_timeout=60 * 60)
def refresh_remote_projects():
    """
    Refresh the details of all RemoteProjects from the remote portal.
    This is called periodically in case we miss the updates that
    come from push notifications
    """
    logger.info("OpenPortal task.refresh_remote_projects")
    now = datetime.datetime.now()
    fail_count = 0

    remote_projects = list(models.RemoteProject.objects.all())

    # randomise the order of the projects to avoid always processing in the same order and potentially
    # leaving some projects with outdated details for a long time
    random.shuffle(remote_projects)

    for remote_project in remote_projects:
        try:
            utils.refresh_remote_project(remote_project)
        except Exception as e:
            logger.error(f"Failed to refresh remote project {remote_project}: {e}")
            fail_count += 1

            if fail_count > 25 and (datetime.datetime.now() - now).seconds > 600:
                logger.error("Too many failures - aborting")
                return
            elif (datetime.datetime.now() - now).seconds > 3600:
                logger.error("sync_remote_projects took too long - aborting")
                return

        if (datetime.datetime.now() - now).seconds > 3600:
            logger.error("sync_remote_projects took too long - aborting")
            return


@shared_task(name="waldur_openportal.sync_remote_storage")
@run_once_task(takeover_timeout=60 * 60)
def sync_remote_storage():
    """
    Fetch and store accumulated storage reports from remote portals for all
    active RemoteAllocations.
    """
    if not config.ensure_config_loaded():
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

            if fail_count > 25 and (datetime.datetime.now() - now).seconds > 600:
                logger.error("Too many failures - aborting")
                return
            elif (datetime.datetime.now() - now).seconds > 3600:
                logger.error("sync_remote_storage took too long - aborting")
                return

        if (datetime.datetime.now() - now).seconds > 3600:
            logger.error("sync_remote_storage took too long - aborting")
            return


@shared_task(name="waldur_openportal.sync_allocation_limits")
@run_once_task(takeover_timeout=60 * 60)
def sync_allocation_limits():
    """
    This task updates the resource limits for all allocations based on project credits
    and current usage. This should be run after sync_usage to ensure all usage data is current.
    """
    if not config.ensure_config_loaded():
        logger.debug(
            "OpenPortal not enabled or config not available, skipping sync_allocation_limits"
        )
        return

    logger.info("OpenPortal task.sync_allocation_limits")
    now = datetime.datetime.now()

    project_credits = list(
        invoice_models.ProjectCredit.objects.select_related("project")
    )

    # randomise the order of the projects to avoid always processing in the same order and potentially
    # leaving some projects with outdated limits for a long time
    random.shuffle(project_credits)

    for project_credit in project_credits:
        # Bound before the try block: the handler below reports on it, and a
        # failure to resolve the project would otherwise raise NameError from
        # inside the handler (or name the previous iteration's project).
        project = None

        try:
            project = project_credit.project

            # Skip fully removed projects
            if project.is_removed:
                continue

            # For projects in grace period or past grace period, set limits to zero
            if project.is_in_grace_period:
                logger.debug(
                    f"Project {project} is in grace period (until {project.end_date_with_grace}) - setting limits to zero"
                )
                credits_available = 0
            elif project.is_expired:
                # Project is expired and past grace period
                logger.debug(
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
            allocations = models.Allocation.objects.filter(
                project=project, is_active=True
            )

            if not allocations:
                logger.debug(
                    f"Project {project} has no OpenPortal allocations - skipping"
                )
                continue

            # Calculate the total usage so far this month across OpenPortal allocations
            # for this project - if it exceeds the number of project credits available
            # then we have to set the limits to zero to prevent any more spend
            if credits_available > 0:
                total_spend = 0.0

                for allocation in allocations:
                    total_spend += float(allocation.node_usage)

                logger.debug(
                    f"Total spend for {project} is {total_spend} hours - {credits_available} available"
                )

                if total_spend >= credits_available:
                    logger.warning(
                        f"Total spend for {project} exceeds available credits - setting limits to zero"
                    )
                    credits_available = 0
        except Exception as e:
            logger.error(
                f"Failed to calculate credits for {project or project_credit}: {e}"
            )
            continue

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

    logger.info(
        f"sync_allocation_limits completed: processed {len(project_credits)} credits"
    )


@shared_task(name="waldur_openportal.sync_remote")
def sync_remote():
    """
    Dispatcher: fans out one sync_remote_for_destination subtask per active destination
    so that destinations are synced in parallel and a down destination cannot block others.
    """
    logger.info("OpenPortal task.sync_remote")

    service_settings_ids = list(
        models.RemoteAllocation.objects.filter(is_active=True)
        .values_list("service_settings_id", flat=True)
        .distinct()
    )

    logger.info(
        f"OpenPortal task.sync_remote: dispatching sync for {len(service_settings_ids)} destination(s)"
    )

    for sid in service_settings_ids:
        try:
            sync_remote_for_destination.delay(sid)
        except Exception as e:
            logger.error(
                f"Failed to dispatch sync_remote_for_destination for service_settings {sid}: {e}"
            )


@shared_task(name="waldur_openportal.sync_remote_for_destination")
@run_once_task(takeover_timeout=60 * 60, include_args=True)
def sync_remote_for_destination(service_settings_id):
    """
    Sync all RemoteAllocations for a single destination (ServiceSettings).
    Allocations are shuffled so that a persistent early failure cannot starve
    later entries across repeated sync cycles.  Failures are counted per
    destination so that a down destination does not consume the failure budget
    of other destinations.
    """
    try:
        service_settings = structure_models.ServiceSettings.objects.get(
            pk=service_settings_id
        )
    except structure_models.ServiceSettings.DoesNotExist:
        logger.error(
            f"sync_remote_for_destination: ServiceSettings {service_settings_id} does not exist"
        )
        return

    logger.info(f"OpenPortal task.sync_remote_for_destination: {service_settings}")

    remote_allocations = list(
        models.RemoteAllocation.objects.filter(
            is_active=True, service_settings=service_settings
        )
    )
    random.shuffle(remote_allocations)

    now = datetime.datetime.now()
    fail_count = 0

    for remote_allocation in remote_allocations:
        if (datetime.datetime.now() - now).seconds > 3600:
            logger.error(
                f"sync_remote_for_destination: {service_settings} took too long - aborting"
            )
            break

        try:
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
                logger.debug(
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
                logger.debug(
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
        except Exception as e:
            logger.error(f"Failed to check remote allocation {remote_allocation}: {e}")
            continue

        try:
            backend = remote_allocation.get_backend()

            if not remote_allocation.is_added_to_openportal():
                logger.info(
                    f"Remote allocation {remote_allocation} not in OpenPortal - adding"
                )
                backend.add_allocated_project(remote_allocation)
            elif remote_allocation.needs_updating():
                logger.debug(
                    f"Remote allocation {remote_allocation} needs updating ({remote_allocation.local_version} vs {remote_allocation.remote_version}) - updating"
                )
                backend.update_allocated_project(remote_allocation, force_update=False)

        except Exception as e:
            logger.error(f"Failed to sync remote project {remote_allocation}: {e}")
            fail_count += 1

            if fail_count > 25 and (datetime.datetime.now() - now).seconds > 600:
                logger.error(
                    f"sync_remote_for_destination: {service_settings} - too many failures, aborting"
                )
                break


@shared_task(name="waldur_openportal.sync_local_users")
@run_once_task(takeover_timeout=60 * 60)
def sync_local_users():
    """
    This task runs through all of the allocations and makes sure that all
    users associated with those allocations are properly synced (e.g.
    added or removed)
    """
    if not config.ensure_config_loaded():
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
def sync_remote_users():
    """
    Dispatcher: fans out one sync_remote_users_for_destination subtask per active
    destination so that a down destination cannot block user syncs for others.
    """
    if not config.ensure_config_loaded():
        logger.debug(
            "OpenPortal not enabled or config not available, skipping sync_remote_users"
        )
        return

    logger.info("OpenPortal task.sync_remote_users")

    service_settings_ids = list(
        models.RemoteAllocation.objects.filter(is_active=True)
        .values_list("service_settings_id", flat=True)
        .distinct()
    )

    logger.info(
        f"OpenPortal task.sync_remote_users: dispatching sync for {len(service_settings_ids)} destination(s)"
    )

    for sid in service_settings_ids:
        try:
            sync_remote_users_for_destination.delay(sid)
        except Exception as e:
            logger.error(
                f"Failed to dispatch sync_remote_users_for_destination for service_settings {sid}: {e}"
            )


@shared_task(name="waldur_openportal.sync_remote_users_for_destination")
@run_once_task(takeover_timeout=60 * 60, include_args=True)
def sync_remote_users_for_destination(service_settings_id):
    """
    Sync users for all RemoteAllocations belonging to a single destination.
    """
    try:
        service_settings = structure_models.ServiceSettings.objects.get(
            pk=service_settings_id
        )
    except structure_models.ServiceSettings.DoesNotExist:
        logger.error(
            f"sync_remote_users_for_destination: ServiceSettings {service_settings_id} does not exist"
        )
        return

    logger.info(
        f"OpenPortal task.sync_remote_users_for_destination: {service_settings}"
    )

    allocations = list(
        models.RemoteAllocation.objects.filter(
            is_active=True, service_settings=service_settings
        )
    )
    random.shuffle(allocations)

    now = datetime.datetime.now()

    for allocation in allocations:
        if (datetime.datetime.now() - now).seconds > 3600:
            logger.error(
                f"sync_remote_users_for_destination: {service_settings} took too long - aborting"
            )
            break

        try:
            sync_remote_allocation_users(allocation)
        except Exception as e:
            logger.error(f"Failed to sync remote users for {allocation}: {e}")


@shared_task(name="waldur_openportal.sync")
@run_once_task(takeover_timeout=60 * 60)
def sync():
    """
    This is a full OpenPortal sync - this will go through all projects
    and ensure that only users associated with those projects have
    the correct associations with any OpenPortal allocations.
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
            logger.debug(f"Skipping project {project} - not a Project instance")
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
                logger.debug(f"Sending notification to {user} in {project}")
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
            logger.debug(f"Skipping project {project} - not a Project instance")
            return

    # find the remote allocations for this project
    remote_allocations = models.RemoteAllocation.objects.filter(
        project=project, is_active=True
    )

    for remote_allocation in remote_allocations:
        try:
            backend = remote_allocation.get_backend()

            logger.debug(f"Updating remote project {remote_allocation}")

            backend.update_allocated_project(remote_allocation)
        except Exception as e:
            logger.error(f"Failed to update remote project {remote_allocation}: {e}")


@shared_task(name="waldur_openportal.apply_membership_control")
def apply_membership_control(
    serialized_remote_project, new_control: str, performed_by_id=None
):
    """
    Apply a membership control transition on a RemoteProject.
    May involve a live fetch from the remote portal (if a member sync is needed),
    so this is run asynchronously rather than blocking the API.
    """
    logger.info(
        f"OpenPortal task.set_membership_control: {serialized_remote_project} -> {new_control!r}"
    )

    if isinstance(serialized_remote_project, models.RemoteProject):
        remote_project = serialized_remote_project
    else:
        remote_project = core_utils.deserialize_instance(serialized_remote_project)
        if not isinstance(remote_project, models.RemoteProject):
            logger.error(
                f"set_membership_control: expected RemoteProject, got {type(remote_project)}"
            )
            return

    performed_by = None
    if performed_by_id is not None:
        performed_by = User.objects.filter(id=performed_by_id).first()

    utils.set_membership_control(
        remote_project,
        new_control=new_control,
        dry_run=False,
        performed_by=performed_by,
    )


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
            logger.debug(f"Skipping project {project} - not a Project instance")
            return

    # find the remote allocations for this project
    remote_allocations = models.RemoteAllocation.objects.filter(
        project=project, is_active=True
    )

    for remote_allocation in remote_allocations:
        try:
            backend = remote_allocation.get_backend()

            logger.debug(f"Deleting remote project {remote_allocation}")

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
        logger.debug(
            f"OpenPortal - ManagedProject {managed_project} is for a removed project"
        )
        raise ValueError(
            f"OpenPortal - ManagedProject {managed_project} is for a removed project"
        )

    # Prevent creating resources for projects past grace period
    if project.is_expired and not project.is_in_grace_period:
        logger.debug(
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
                logger.debug(
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
            logger.debug(
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
                    logger.debug(
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


def update_award(
    board: OpenPortalBoard,
    project: openportal.ProjectIdentifier,
    details: openportal.AwardDetails,
    force_approve: bool = False,
) -> openportal.ProjectMapping:
    """
    Update the project in the OpenPortal board with the given details.
    If the project does not exist, then there will be an error.
    """
    mapping = board.update_award(project, details, force_approve=force_approve)

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


def create_award(
    board: OpenPortalBoard,
    identifier: openportal.ProjectIdentifier,
    details: openportal.AwardDetails,
) -> openportal.ProjectMapping:
    """
    Create a project in the OpenPortal board with the given identifier and details.
    """
    # first, create the project if it doesn't exist
    mapping = board.create_award(identifier, details)

    # next, update the details of the project to match the details provided.
    # This will also create the default resources for the project
    mapping = update_award(board, mapping.project, details)

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

    if not managed_project.is_approved():
        logger.error(
            f"OpenPortal - ManagedProject {managed_project} is not approved - cannot call handler!"
        )
        raise ValueError(
            f"OpenPortal - ManagedProject {managed_project} is not approved - cannot call handler!"
        )

    board = OpenPortalBoard(managed_project.get_destination())
    identifier = managed_project.get_remote_identifier()
    details = managed_project.get_details()

    result = update_award(board, identifier, details, force_approve=True)

    logger.info(
        f"OpenPortal - Managed project {managed_project} approved - mapping is {result}"
    )

    managed_project.notify_accepted()


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
        logger.error(
            f"OpenPortal - {_trim_job(job)} is not a Job instance - it is {type(job)}"
        )
        return

    job_model = job

    if job_model.state != models.Job.State.PENDING:
        logger.debug(f"OpenPortal - Job {job.job_id} is not pending - skipping")
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
        logger.debug(f"OpenPortal - Job {job_model.job_id} is not pending - skipping")
        return

    job_model.state = models.Job.State.RUNNING
    job_model.save()

    board = OpenPortalBoard(
        job.forwarded_for if job.forwarded_for is not None else job.destination
    )

    logger.info(f"Running job {_trim_job(job)} - status {job.state}")

    command = job.instruction.command
    args = job.instruction.arguments

    try:
        result = None

        if command == "create_project" or command == "create_award":
            identifier = openportal.ProjectIdentifier(args[0])
            details = openportal.AwardDetails(args[1])
            result = create_award(board, identifier, details)
        elif command == "remove_project" or command == "remove_award":
            identifier = openportal.ProjectIdentifier(args[0])
            result = board.remove_award(identifier)
        elif command == "update_project" or command == "update_award":
            identifier = openportal.ProjectIdentifier(args[0])
            details = openportal.AwardDetails(args[1])
            result = update_award(board, identifier, details)
        elif command == "get_project" or command == "get_award":
            identifier = openportal.ProjectIdentifier(args[0])
            result = board.get_award(identifier)
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


@shared_task(name="waldur_openportal.refresh_remote_award")
def refresh_remote_award(destination: str, local_identifier: str):
    """
    Re-fetch the current AwardDetails for a RemoteProject from the remote portal
    and update last_confirmed_details.  Always updates last_contact_time.
    If the fetch fails, last_confirmed_details is left unchanged.

    local_identifier is the award identifier on this portal, e.g. "awardtest.ukri".
    The RemoteProject is located via destination + the local project slug, then the
    remote identifier stored on that record is used for the actual fetch.
    """
    logger.info(
        f"OpenPortal task.refresh_remote_award: destination={destination!r}, local_identifier={local_identifier!r}"
    )

    if not config.ensure_config_loaded():
        return

    try:
        destination: openportal.Destination = openportal.Destination(destination)
    except Exception as e:
        logger.error(f"refresh_remote_award: invalid destination {destination!r}: {e}")
        return

    local_id = openportal.ProjectIdentifier(local_identifier)

    if str(local_id.portal) != str(openportal.get_portal()):
        logger.error(
            f"refresh_remote_award: identifier {local_identifier!r} is for portal "
            f"{local_id.portal!r}, expected {openportal.get_portal()!r} — ignoring"
        )
        return

    current_project = None

    try:
        # now find the project by the shortname
        project_info = models.ProjectInfo.objects.get(shortname=str(local_id.project))
        current_project = project_info.project
    except models.ProjectInfo.DoesNotExist:
        pass

    if current_project is None:
        # look this up by the slug instead of the shortname
        try:
            current_project = structure_models.Project.objects.get(
                slug=str(local_id.project)
            )
        except structure_models.Project.DoesNotExist:
            logger.error(
                f"refresh_remote_award: no Project found with slug {local_id.project!r}"
            )
            return

    try:
        remote_project = models.RemoteProject.objects.get(
            destination=str(destination), current_project=current_project
        )
    except models.RemoteProject.DoesNotExist:
        logger.error(
            f"refresh_remote_award: no RemoteProject found for "
            f"destination={destination!r}, project ID={local_id.project!r}"
        )
        return

    utils.refresh_remote_project(remote_project)


def dispatch_notification(notification: openportal.Notification):
    """
    Dispatch an OpenPortal bridge notification to the appropriate handler.
    Called synchronously from the fetch_notification view.
    Notifications are fire-and-forget — errors are logged but not raised.
    """
    _NOTIFICATION_HANDLERS = {
        "award_added": _handle_award_added,
        "award_removed": _handle_award_removed,
        "award_changed": _handle_award_changed,
        "award_accepted": _handle_award_accepted,
        "award_rejected": _handle_award_rejected,
    }

    if notification.event_type is None:
        logger.error(f"OpenPortal notification has no event type: {notification}")
        return

    handler = _NOTIFICATION_HANDLERS.get(notification.event_type)
    if handler is None:
        return

    handler(notification)


def _schedule_award_task_if_local(notification: openportal.Notification, task):
    """
    Parse the notification's event_argument as a ProjectIdentifier, check that
    it belongs to this portal, and if so schedule the given Celery task with
    (destination, event_argument) arguments.
    Returns early without scheduling if the identifier is for a different portal.
    """
    if not config.ensure_config_loaded():
        return

    local_id = openportal.ProjectIdentifier(str(notification.event_argument))
    if str(local_id.portal) != str(openportal.get_portal()):
        logger.warning(
            f"Ignoring notification {notification.event_type!r}: identifier "
            f"{notification.event_argument!r} belongs to portal {local_id.portal!r}, "
            f"not {openportal.get_portal()!r}"
        )
        return

    try:
        destination = openportal.Destination(str(notification.destination))
    except Exception as e:
        logger.error(
            f"Invalid destination in notification: {notification.destination!r}: {e}"
        )
        return

    # reverse the destination as notifications are reversed
    destination = destination.reverse()

    task.delay(str(destination), str(notification.event_argument))


def _schedule_refresh_award_if_local(notification: openportal.Notification):
    _schedule_award_task_if_local(notification, refresh_remote_award)


@shared_task(name="waldur_openportal.reject_remote_award")
def reject_remote_award(destination: str, local_identifier: str):
    """
    Find the RemoteProject for this award and transition it (and its
    RemoteAllocation) to ERROR/ERRED state.
    Called when the remote portal sends an award_rejected notification.
    """
    logger.info(
        f"OpenPortal task.reject_remote_award: destination={destination!r},"
        f" local_identifier={local_identifier!r}"
    )

    if not config.ensure_config_loaded():
        return

    local_id = openportal.ProjectIdentifier(local_identifier)

    if str(local_id.portal) != str(openportal.get_portal()):
        logger.error(
            f"reject_remote_award: identifier {local_identifier!r} is for portal "
            f"{local_id.portal!r}, expected {openportal.get_portal()!r} — ignoring"
        )
        return

    try:
        project = structure_models.Project.objects.get(slug=str(local_id.project))
    except structure_models.Project.DoesNotExist:
        logger.error(
            f"reject_remote_award: no Project found with slug {local_id.project!r}"
        )
        return

    try:
        dest = openportal.Destination(destination)
    except Exception as e:
        logger.error(f"reject_remote_award: invalid destination {destination!r}: {e}")
        return

    try:
        remote_project = models.RemoteProject.objects.get(
            destination=str(dest), current_project=project
        )
    except models.RemoteProject.DoesNotExist:
        logger.error(
            f"reject_remote_award: no RemoteProject found for "
            f"destination={dest!r}, project slug={local_id.project!r}"
        )
        return

    try:
        remote_project.record_rejected("Award rejected by remote portal")
    except Exception as e:
        logger.warning(
            f"reject_remote_award: failed to record rejection for {remote_project}: {e}"
        )
        return

    remote_project_service.touch_last_contact(remote_project)


@shared_task(name="waldur_openportal.accept_remote_award")
def accept_remote_award(destination: str, local_identifier: str):
    """
    Find the RemoteProject for this award, refetch the confirmed details
    from the remote portal, then transition the project (and its
    RemoteAllocation) to ACTIVE/OK state.
    Called when the remote portal sends an award_accepted notification.
    """
    logger.info(
        f"OpenPortal task.accept_remote_award: destination={destination!r},"
        f" local_identifier={local_identifier!r}"
    )

    if not config.ensure_config_loaded():
        return

    local_id = openportal.ProjectIdentifier(local_identifier)

    if str(local_id.portal) != str(openportal.get_portal()):
        logger.error(
            f"accept_remote_award: identifier {local_identifier!r} is for portal "
            f"{local_id.portal!r}, expected {openportal.get_portal()!r} — ignoring"
        )
        return

    try:
        project = structure_models.Project.objects.get(slug=str(local_id.project))
    except structure_models.Project.DoesNotExist:
        logger.error(
            f"accept_remote_award: no Project found with slug {local_id.project!r}"
        )
        return

    try:
        dest = openportal.Destination(destination)
    except Exception as e:
        logger.error(f"accept_remote_award: invalid destination {destination!r}: {e}")
        return

    try:
        remote_project = models.RemoteProject.objects.get(
            destination=str(dest), current_project=project
        )
    except models.RemoteProject.DoesNotExist:
        logger.error(
            f"accept_remote_award: no RemoteProject found for "
            f"destination={dest!r}, project slug={local_id.project!r}"
        )
        return

    confirmed_details_json = None
    try:
        board = OpenPortalBoard(dest)
        details = board.refetch_award(local_id)
        if details:
            confirmed_details_json = json.loads(details.to_json())
    except Exception as e:
        logger.warning(
            f"accept_remote_award: could not refetch award details for "
            f"{remote_project} — accepting without confirmed details: {e}"
        )

    try:
        remote_project.record_accepted(confirmed_details_json=confirmed_details_json)
    except Exception as e:
        logger.warning(
            f"accept_remote_award: failed to record acceptance for "
            f"{remote_project}: {e}"
        )
        return

    remote_project_service.touch_last_contact(remote_project)


def _handle_award_added(notification: openportal.Notification):
    logger.debug(f"OpenPortal notification: {notification}")
    _schedule_refresh_award_if_local(notification)


def _handle_award_removed(notification: openportal.Notification):
    logger.debug(f"OpenPortal notification: {notification}")
    _schedule_refresh_award_if_local(notification)


def _handle_award_changed(notification: openportal.Notification):
    logger.debug(f"OpenPortal notification: {notification}")
    _schedule_refresh_award_if_local(notification)


def _handle_award_accepted(notification: openportal.Notification):
    logger.info(f"OpenPortal notification: {notification}")
    _schedule_award_task_if_local(notification, accept_remote_award)


def _handle_award_rejected(notification: openportal.Notification):
    logger.info(f"OpenPortal notification: {notification}")
    _schedule_award_task_if_local(notification, reject_remote_award)


@shared_task(name="waldur_openportal.sync_offering_agents")
def sync_offering_agents():
    """
    This task is called to sync the agents for all offerings
    that are associated with remote OpenPortal backends.
    """
    if not config.ensure_config_loaded():
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
    This task polls the OpenPortal jobs board to see if this portal
    has received any jobs. If it has, then it pulls the job from the
    board and then spawns a new task to process the job.
    """
    if not config.ensure_config_loaded():
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
                logger.debug(f"Job {job.id} is not pending - skipping")
                continue

            logger.info(f"Processing job {_trim_job(job)} from OpenPortal board")
            j = models.Job.objects.create(
                id=str(job.id),
                data=job.to_json(),
            )

            run_job.delay(core_utils.serialize_instance(j))

        except Exception as e:
            logger.error(f"Failed to process job {job.id}: {e}")
            continue


@shared_task(name="waldur_openportal.clean_stale_jobs")
def clean_stale_jobs(days: int = 2, batch_size: int = 5000):
    """
    This task deletes all OpenPortal jobs that were created more than
    {days} days ago - this is to prevent the database from filling up with
    old jobs that are no longer relevant.
    """
    logger.info("OpenPortal task.clean_stale_jobs")
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    total = 0
    batch_count = 0
    while True:
        batch_ids = list(
            models.Job.objects.filter(created__lt=cutoff).values_list("id", flat=True)[
                :batch_size
            ]
        )
        if not batch_ids:
            break
        batch_count += 1
        logger.info(f"Deleting batch {batch_count} with {len(batch_ids)} jobs")
        models.Job.objects.filter(id__in=batch_ids).delete()
        total += len(batch_ids)
    logger.info(f"Deleted {total} jobs older than {days} days")


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

        try:
            utils.fix_total_allocation(project)
        except Exception as e:
            logger.error(f"Failed to fix total allocation for project {project}: {e}")


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
            "site_name": constance_config.SITE_NAME,
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


@shared_task(name="waldur_openportal.mark_stale_remote_projects")
@run_once_task(takeover_timeout=60 * 60)
def mark_stale_remote_projects():
    """
    Mark RemoteProjects as STALE when we have not received any contact
    from the remote portal for more than STALE_THRESHOLD_HOURS hours.

    This covers two cases:
      - last_contact_time is set but is older than the threshold
      - last_contact_time is null and the project was created more than
        the threshold ago (i.e. we never heard back after creation)

    A STALE project transitions back to ACTIVE automatically when
    touch_last_contact() is called (e.g. on a successful usage or
    storage report fetch).
    """
    STALE_THRESHOLD_HOURS = 12
    logger.info("OpenPortal task.mark_stale_remote_projects")

    threshold = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        hours=STALE_THRESHOLD_HOURS
    )

    # Projects that were contacted before the threshold
    stale_by_contact = models.RemoteProject.objects.filter(
        state=models.RemoteProjectState.ACTIVE,
        last_contact_time__lt=threshold,
    )

    # Projects that were never contacted and were created before the threshold
    stale_by_silence = models.RemoteProject.objects.filter(
        state=models.RemoteProjectState.ACTIVE,
        last_contact_time__isnull=True,
        created__lt=threshold,
    )

    stale_projects = list(stale_by_contact) + list(stale_by_silence)

    for remote_project in stale_projects:
        try:
            remote_project.state = models.RemoteProjectState.STALE
            remote_project.save(update_fields=["state", "modified"])
            models.RemoteProjectAuditEntry.objects.create(
                remote_project=remote_project,
                event_type=models.RemoteProjectAuditEventType.STATE_CHANGED,
                note=(
                    f"Marked STALE: no contact from remote portal "
                    f"for more than {STALE_THRESHOLD_HOURS} hours."
                ),
            )
            logger.info(f"Marked RemoteProject {remote_project} as STALE")
        except Exception as e:
            logger.error(f"Failed to mark RemoteProject {remote_project} as STALE: {e}")
