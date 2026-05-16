import hashlib
import json
import logging
import traceback
from uuid import uuid4

from celery import Task as CeleryTask
from celery import current_app, shared_task
from celery.app.task import _reprtask
from celery.local import Proxy
from celery.result import AsyncResult
from celery.worker.request import Request
from constance import config
from django.core.cache import cache
from django.db import IntegrityError, OperationalError, close_old_connections
from django.db import models as django_models
from django.db.models import ObjectDoesNotExist
from django.utils import timezone
from django_fsm import TransitionNotAllowed

from waldur_core.core import models, utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.exceptions import RuntimeStateException

logger = logging.getLogger(__name__)


class StateChangeError(RuntimeError):
    pass


class TaskType(type):
    """
    Meta class for tasks.
    Automatically registers the task in the task registry (except
    if the :attr:`Task.abstract`` attribute is set).
    If no :attr:`Task.name` attribute is provided, then the name is generated
    from the module and class name.
    Taken from https://github.com/celery/celery/blob/4.3/celery/task/base.py
    """

    _creation_count = {}  # used by old non-abstract task classes

    def __new__(cls, name, bases, attrs):
        new = super().__new__
        task_module = attrs.get("__module__") or "__main__"

        # - Abstract class: abstract attribute shouldn't be inherited.
        abstract = attrs.pop("abstract", None)
        if abstract or not attrs.get("autoregister", True):
            return new(cls, name, bases, attrs)

        # The 'app' attribute is now a property, with the real app located
        # in the '_app' attribute.  Previously this was a regular attribute,
        # so we should support classes defining it.
        app = attrs.pop("_app", None) or attrs.pop("app", None)

        # Attempt to inherit app from one the bases
        if not isinstance(app, Proxy) and app is None:
            for base in bases:
                if getattr(base, "_app", None):
                    app = base._app
                    break
            else:
                app = current_app._get_current_object()
        attrs["_app"] = app

        # - Automatically generate missing/empty name.
        task_name = attrs.get("name")
        if not task_name:
            attrs["name"] = task_name = app.gen_task_name(name, task_module)

        # - Create and register class.
        # Because of the way import happens (recursively)
        # we may or may not be the first time the task tries to register
        # with the framework.  There should only be one class for each task
        # name, so we always return the registered version.
        tasks = app._tasks
        if task_name not in tasks:
            tasks.register(new(cls, name, bases, attrs))
        instance = tasks[task_name]
        instance.bind(app)
        return instance.__class__

    def __repr__(self):
        return _reprtask(self)


class Task(CeleryTask, metaclass=TaskType):
    """Base class for tasks that are run by executors.

    Provides standard way for input data deserialization.
    """

    @classmethod
    def get_description(cls, *args, **kwargs):
        """Add additional information about task to celery logs.

        Receives same parameters as method "run".
        """
        raise NotImplementedError()

    def run(self, serialized_instance, *args, **kwargs):
        """Deserialize input data and start backend operation execution"""
        try:
            instance = utils.deserialize_instance(serialized_instance)
        except ObjectDoesNotExist as e:
            # Log the error for debugging while preserving original exception details
            logger.warning(
                "Task %s failed to deserialize instance %s: %s. Object was likely deleted between task queuing and execution.",
                self.__class__.__name__,
                serialized_instance,
                str(e),
            )
            raise ObjectDoesNotExist(
                "Cannot restore instance from serialized object %s. Probably it was deleted."
                % serialized_instance
            ) from e

        self.args = args
        self.kwargs = kwargs

        self.pre_execute(instance)
        result = self.execute(instance, *self.args, **self.kwargs)
        self.post_execute(instance)
        if result and isinstance(result, django_models.Model):
            result = utils.serialize_instance(result)
        return result

    def pre_execute(self, instance):
        pass

    def execute(self, instance, *args, **kwargs):
        """Execute backend operation"""
        raise NotImplementedError(
            "%s should implement method `execute`" % self.__class__.__name__
        )

    def post_execute(self, instance):
        pass


class EmptyTask(CeleryTask, metaclass=TaskType):
    def run(self, *args, **kwargs):
        pass


class StateTransitionTask(Task):
    """Execute instance state transition, changes instance action and action details if defined.

    It is impossible to change object action without state transition.
    """

    @classmethod
    def get_description(cls, instance, *args, **kwargs):
        transition_method = kwargs.get("state_transition")
        return (
            f'Change state of object "{instance}" using method "{transition_method}".'
        )

    def state_transition(
        self, instance, transition_method, action=None, action_details=None
    ):
        instance_description = (
            f"{instance.__class__.__name__} instance `{instance}` (PK: {instance.pk})"
        )
        old_state = instance.get_state_display()
        try:
            getattr(instance, transition_method)()
            if action is not None:
                instance.action = action
            if action_details is not None:
                instance.action_details = action_details
            instance.save()
        except IntegrityError:
            message = f"Could not change state of {instance_description}, using method `{transition_method}` due to concurrent update"
            raise StateChangeError(message)
        except TransitionNotAllowed:
            message = f"Could not change state of {instance_description}, using method `{transition_method}`. Current instance state: {instance.get_state_display()}."
            raise StateChangeError(message)
        else:
            logger.info(
                "State of %s changed from %s to %s, with method `%s`",
                instance_description,
                old_state,
                instance.get_state_display(),
                transition_method,
            )

    def pre_execute(self, instance):
        state_transition = self.kwargs.pop("state_transition", None)
        action = self.kwargs.pop("action", None)
        action_details = self.kwargs.pop("action_details", None)
        if state_transition is not None:
            self.state_transition(instance, state_transition, action, action_details)
        super().pre_execute(instance)

    # Empty execute method allows to use StateTransitionTask as standalone task
    def execute(self, instance, *args, **kwargs):
        return instance


class RuntimeStateChangeTask(Task):
    """Allows to change runtime state of instance before and after execution.

    Define kwargs:
     - runtime_state - to change instance runtime state during execution.
     - success_runtime_state - to change instance runtime state after success tasks execution.
    """

    @classmethod
    def get_description(cls, instance, *args, **kwargs):
        runtime_state = kwargs.get("runtime_state")
        return f'Change runtime state of object "{instance}" to "{runtime_state}".'

    def update_runtime_state(self, instance, runtime_state):
        instance.runtime_state = runtime_state
        instance.save(update_fields=["runtime_state"])

    def pre_execute(self, instance):
        self.runtime_state = self.kwargs.pop("runtime_state", None)
        self.success_runtime_state = self.kwargs.pop("success_runtime_state", None)

        if self.runtime_state is not None:
            self.update_runtime_state(instance, self.runtime_state)
        super().pre_execute(instance)

    def post_execute(self, instance, *args, **kwargs):
        if self.success_runtime_state is not None:
            self.update_runtime_state(instance, self.success_runtime_state)
        super().post_execute(instance)

    # Empty execute method allows to use RuntimeStateChangeTask as standalone task
    def execute(self, instance, *args, **kwargs):
        return instance


class ResetErrorMessageTask(Task):
    def pre_execute(self, instance):
        if isinstance(instance, models.ErrorMessageMixin) and instance.error_message:
            instance.error_message = ""
            instance.error_traceback = ""
            instance.save(update_fields=["error_message", "error_traceback"])

        super().pre_execute(instance)


class BackendMethodTask(
    RuntimeStateChangeTask, StateTransitionTask, ResetErrorMessageTask
):
    """Execute method of instance backend"""

    @classmethod
    def get_description(cls, instance, backend_method, *args, **kwargs):
        actions = [f'Run backend method "{backend_method}" for instance "{instance}".']
        if "state_transition" in kwargs:
            actions.append(
                StateTransitionTask.get_description(
                    instance, backend_method, *args, **kwargs
                )
            )
        if "runtime_state" in kwargs:
            actions.append(
                RuntimeStateChangeTask.get_description(
                    instance, backend_method, *args, **kwargs
                )
            )
        return " ".join(actions)

    def get_backend(self, instance):
        return instance.get_backend()

    def execute(self, instance, backend_method, *args, **kwargs):
        backend = self.get_backend(instance)
        getattr(backend, backend_method)(instance, *args, **kwargs)


class IndependentBackendMethodTask(BackendMethodTask):
    """Execute instance backend method that does not receive instance as argument"""

    def execute(self, instance, backend_method, *args, **kwargs):
        backend = self.get_backend(instance)
        getattr(backend, backend_method)(*args, **kwargs)


class DeletionTask(Task):
    """Delete instance"""

    @classmethod
    def get_description(cls, *args, **kwargs):
        instance = args[0]
        return 'Delete instance "%s".' % instance

    def execute(self, instance):
        instance_description = (
            f"{instance.__class__.__name__} instance `{instance}` (PK: {instance.pk})"
        )
        instance.delete()
        logger.info("%s was successfully deleted", instance_description)


class ErrorMessageTask(Task):
    """Store error in error_message field.

    This task should not be called as immutable, because it expects result_uuid
    as input argument.
    """

    @classmethod
    def get_description(cls, result_id, instance, *args, **kwargs):
        return 'Add error message to instance "%s".' % instance

    def run(self, result_id, serialized_instance, *args, **kwargs):
        self.result = self.AsyncResult(result_id)
        return super().run(serialized_instance, *args, **kwargs)

    def save_error_message(self, instance):
        if isinstance(instance, models.ErrorMessageMixin):
            try:
                error_message = instance.error_message or self.result.result or ""
                error_traceback = str(self.result.traceback)
            except AttributeError as ex:
                error_message = f"Internal error: {ex}"
                error_traceback = traceback.format_exc()

            instance.error_message = error_message
            instance.error_traceback = error_traceback

            instance.save(update_fields=["error_message", "error_traceback"])

            # log exception if instance is not already ERRED.
            if instance.state != CoreStates.ERRED:
                message = "Instance: %s.\n" % utils.serialize_instance(instance)
                message += "Error: %s.\n" % error_message
                message += error_traceback
                logger.exception(message)

    def execute(self, instance):
        self.save_error_message(instance)


class ErrorStateTransitionTask(ErrorMessageTask, StateTransitionTask):
    """Set instance as erred and save error message.

    This task should not be called as immutable, because it expects result_uuid
    as input argument.
    """

    @classmethod
    def get_description(cls, result_id, instance, *args, **kwargs):
        return 'Add error message and set erred instance "%s".' % instance

    def execute(self, instance):
        self.save_error_message(instance)
        self.state_transition(instance, "set_erred", action="", action_details={})


class RecoverTask(StateTransitionTask):
    """Change instance state from ERRED to OK and clear error_message"""

    @classmethod
    def get_description(cls, instance, *args, **kwargs):
        return 'Recover instance "%s".' % instance

    def execute(self, instance):
        self.state_transition(instance, "recover")
        instance.error_message = ""
        instance.error_traceback = ""
        instance.save(update_fields=["error_message", "error_traceback"])


class PreApplyExecutorTask(Task):
    """Run executor as a task"""

    @classmethod
    def get_description(cls, executor, instance, *args, **kwargs):
        return f'Run executor "{executor}" for instance "{instance}".'

    def run(self, serialized_executor, serialized_instance, *args, **kwargs):
        self.executor = utils.deserialize_class(serialized_executor)
        return super().run(serialized_instance, *args, **kwargs)

    def execute(self, instance, **kwargs):
        self.executor.pre_apply(instance, **kwargs)


class BackgroundTask(CeleryTask, metaclass=TaskType):
    """Task that is run in background via celerybeat.

    Background task features:
     - background task does not start if previous task with the same name
       and input parameters is not completed yet;
     - all background tasks are scheduled in separate queue "background-durable";
     - by default we do not log background tasks in celery logs. So tasks
       should log themselves explicitly and make sure that they will not
       spam error messages;
     - prevents queue overflow and eliminates O(N) worker inspection by using cache with TTL.
    """

    is_background = True

    # Safety net: If worker crashes hard (SIGKILL), lock auto-expires after this time.
    # Set generously above CELERY_TASK_TIME_LIMIT to account for queue wait time
    # and scheduling delays. Current CELERY_TASK_TIME_LIMIT is 30 min.
    lock_timeout = 60 * 60 * 2  # 2 hours default

    def get_unique_key(self, args, kwargs):
        """
        Generate a unique lock ID.
        Override this in subclasses to ignore specific args.
        """
        # Default: Hash task name + args. Ignore kwargs by default to be safe.
        # For Waldur resources, usually args[0] (serialized resource) is enough.
        # Safe JSON serialization
        try:
            payload_str = json.dumps(args, sort_keys=True)
        except (TypeError, ValueError):
            payload_str = str(args)

        payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
        return f"celery-lock:{self.name}:{payload_hash}"

    def apply_async(self, args=None, kwargs=None, **options):
        args = args or ()
        kwargs = kwargs or {}

        # 1. Generate Key
        lock_key = self.get_unique_key(args, kwargs)

        # 2. Check Lock (Atomic ADD)
        # We store the task_id inside the lock for debugging purposes
        task_id = options.get("task_id") or str(uuid4())

        # cache.add returns True if key was set, False if key already existed.
        # celery-beat is long-lived and has no HTTP request boundary, so Django
        # does not recycle its DB connection. If Postgres drops it (idle
        # timeout, restart, pgbouncer recycle), DatabaseCache calls raise
        # OperationalError forever. Recycle once and retry transparently.
        acquired = self._cache_add_with_retry(lock_key, task_id)
        if not acquired:
            logger.info(
                "Skipping task %s (args=%s) - Lock exists: %s",
                self.name,
                args,
                lock_key,
            )
            # Return dummy result to satisfy Celery Beat
            return self.AsyncResult(task_id)

        # 3. Schedule Task
        try:
            # We inject the lock key into headers so the worker knows what to delete
            headers = options.get("headers", {})
            headers["__waldur_lock_key"] = lock_key
            options["headers"] = headers

            return super().apply_async(args=args, kwargs=kwargs, **options)
        except Exception:
            # If connection to Broker fails, release lock immediately
            cache.delete(lock_key)
            raise

    def _cache_add_with_retry(self, lock_key, task_id):
        try:
            return cache.add(lock_key, task_id, timeout=self.lock_timeout)
        except OperationalError:
            logger.warning(
                "Stale DB connection while acquiring lock for %s; recycling and retrying",
                self.name,
            )
            close_old_connections()
            return cache.add(lock_key, task_id, timeout=self.lock_timeout)

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        """
        Cleanup handler.
        Runs on Success, Failure, and SoftTimeLimitExceeded.
        Does NOT run on SIGKILL (Hard Crash).
        """
        lock_key = self.request.headers.get("__waldur_lock_key")

        if lock_key:
            # Check if we own the lock before deleting
            # (prevents race condition if lock expired and was re-acquired by another worker)
            current_lock_holder = cache.get(lock_key)
            if current_lock_holder == task_id:
                cache.delete(lock_key)
                logger.debug("Released lock for task %s: %s", self.name, lock_key)
            else:
                logger.debug(
                    "Lock not owned by task %s (owner: %s), skipping release: %s",
                    task_id,
                    current_lock_holder,
                    lock_key,
                )

        super().after_return(status, retval, task_id, args, kwargs, einfo)


def log_celery_task(request):
    """Add description to celery log output"""
    task = request.task
    description = None
    if isinstance(task, Task):
        try:
            args, kwargs, embed = request._payload
            description = task.get_description(*args, **kwargs)
        except NotImplementedError:
            pass
        except Exception as e:
            # Logging should never break workflow.
            logger.exception(
                f"Cannot get description for task {task.__class__.__name__}. Error: {e}"
            )

    return "{0.name}[{0.id}]{1}{2}{3}".format(
        request,
        f" {description}" if description else "",
        f" eta:[{request.eta}]" if request.eta else "",
        f" expires:[{request.expires}]" if request.expires else "",
    )


# XXX: drop the hack and use shadow name in celery 4.0
Request.__str__ = log_celery_task


class PollRuntimeStateTask(Task):
    max_retries = 1200
    default_retry_delay = 5

    @classmethod
    def get_description(cls, instance, backend_pull_method, *args, **kwargs):
        return f'Poll instance "{instance}" with method "{backend_pull_method}"'

    def get_backend(self, instance):
        return instance.get_backend()

    def _get_instance_info(self, instance):
        """Safely extract instance information for logging."""
        info = {
            "pk": getattr(instance, "pk", "unknown"),
            "class": instance.__class__.__name__,
        }

        # Safely get UUID if available
        if hasattr(instance, "uuid") and instance.uuid:
            info["uuid"] = str(instance.uuid)

        # Safely get name if available
        if hasattr(instance, "name") and instance.name:
            info["name"] = instance.name

        return info

    def execute(
        self,
        instance,
        backend_pull_method,
        success_state,
        erred_state,
        deleted_state=None,
    ):
        backend = self.get_backend(instance)
        getattr(backend, backend_pull_method)(instance)
        instance.refresh_from_db()
        if instance.runtime_state not in (success_state, erred_state, deleted_state):
            instance_info = self._get_instance_info(instance)
            uuid_part = (
                f", UUID: {instance_info['uuid']}" if "uuid" in instance_info else ""
            )
            name_part = (
                f', name: "{instance_info["name"]}"' if "name" in instance_info else ""
            )
            logger.info(
                f"Instance {instance_info['class']} (PK: {instance_info['pk']}"
                f"{uuid_part}{name_part}"
                f") runtime state ({instance.runtime_state}) is not matching "
                f"success ({success_state}), erred ({erred_state}) or deleted ({deleted_state}) states, retrying."
            )
            self.retry()
        elif instance.runtime_state == erred_state:
            instance_info = self._get_instance_info(instance)
            uuid_part = (
                f", UUID: {instance_info['uuid']}" if "uuid" in instance_info else ""
            )
            name_part = (
                f', name: "{instance_info["name"]}"' if "name" in instance_info else ""
            )
            raise RuntimeStateException(
                f"{instance_info['class']} (PK: {instance_info['pk']}"
                f"{uuid_part}{name_part}"
                f") runtime state become erred: {erred_state}"
            )
        return instance


class PollStateTask(Task):
    max_retries = 1200
    default_retry_delay = 5

    def execute(self, instance, *args, **kwargs):
        if instance.state not in (
            CoreStates.OK,
            CoreStates.ERRED,
        ):
            self.retry()


class PollBackendCheckTask(Task):
    max_retries = 600
    default_retry_delay = 5

    @classmethod
    def get_description(cls, instance, backend_check_method, *args, **kwargs):
        return f'Check instance "{instance}" with method "{backend_check_method}"'

    def get_backend(self, instance):
        return instance.get_backend()

    def execute(self, instance, backend_check_method):
        # backend_check_method should return True if object does not exist at backend
        backend = self.get_backend(instance)
        if not getattr(backend, backend_check_method)(instance):
            self.retry()
        return instance


class ExtensionTaskMixin(CeleryTask, metaclass=TaskType):
    """
    This mixin allows to skip task scheduling if extension is disabled.
    Subclasses should implement "is_extension_disabled" method which returns boolean value.
    """

    def is_extension_disabled(self):
        return False

    def apply_async(self, args=None, kwargs=None, **options):
        if self.is_extension_disabled():
            message = (
                "Task %s is not scheduled, because its extension is disabled."
                % self.name
            )
            logger.info(message)
            return self.AsyncResult(options.get("task_id") or str(uuid4()))
        return super().apply_async(args=args, kwargs=kwargs, **options)


@shared_task(name="waldur_core.core.cleanup_expired_personal_access_tokens")
def cleanup_expired_personal_access_tokens():
    """Deactivate expired PATs."""
    from waldur_core.core.models import PersonalAccessToken
    from waldur_core.logging import event_logger
    from waldur_core.logging.enums import EventType

    expired = PersonalAccessToken.objects.filter(
        expires_at__lte=timezone.now(), is_active=True
    )
    for pat in expired.select_related("user"):
        event_logger.emit(
            "Personal access token {pat_name} for user {affected_user_username} has expired.",
            event_type=EventType.PAT_EXPIRED,
            event_context={"affected_user": pat.user, "pat_name": pat.name},
            scopes=[pat.user],
        )
    count = expired.update(is_active=False)
    if count:
        logger.info("Deactivated %d expired personal access tokens.", count)


@shared_task(name="waldur_core.reset_updating_resources")
def reset_updating_resources():
    """Reset resources stuck in UPDATING state when their Celery tasks are completed."""
    for model in models.ActionMixin.get_all_models():
        for instance in model.objects.filter(state=CoreStates.UPDATING).exclude(
            task_id=None
        ):
            async_result = AsyncResult(instance.task_id)
            if async_result.ready():
                instance_description = f"{instance.__class__.__name__} instance `{instance}` (PK: {instance.pk})"
                logger.info(
                    "State of %s changed from UPDATING to OK because Celery task is ready.",
                    instance_description,
                )
                instance.set_ok()
                instance.task_id = None
                instance.save(update_fields=["state", "task_id"])


@shared_task(name="waldur_core.sample_table_sizes")
def sample_table_sizes():
    """
    Sample all database table sizes and store them for trend analysis.
    This task runs daily to collect historical data for detecting abnormal growth patterns.
    """
    from django.db import connection

    if not config.TABLE_GROWTH_MONITORING_ENABLED:
        logger.info("Table growth monitoring is disabled, skipping sample_table_sizes")
        return

    today = timezone.now().date()
    min_size_bytes = config.TABLE_GROWTH_MIN_SIZE_BYTES

    # Query PostgreSQL for table sizes and row estimates
    sql = """
    SELECT
        pg_statio_user_tables.relname AS table_name,
        pg_total_relation_size(pg_statio_user_tables.relid) AS total_size,
        pg_relation_size(pg_statio_user_tables.relid) AS data_size,
        pg_stat_user_tables.n_live_tup AS row_estimate
    FROM
        pg_catalog.pg_statio_user_tables
    LEFT JOIN
        pg_stat_user_tables ON pg_statio_user_tables.relid = pg_stat_user_tables.relid
    WHERE
        pg_total_relation_size(pg_statio_user_tables.relid) >= %s
    ORDER BY
        pg_total_relation_size(pg_statio_user_tables.relid) DESC;
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, [min_size_bytes])
        rows = cursor.fetchall()

    count = 0
    for row in rows:
        table_name, total_size, data_size, row_estimate = row
        models.DailyTableSizeHistory.objects.update_or_create(
            table_name=table_name,
            date=today,
            defaults={
                "total_size": total_size,
                "data_size": data_size,
                "row_estimate": row_estimate,
            },
        )
        count += 1

    logger.info(
        "Sampled table sizes for %d tables on %s",
        count,
        today,
    )

    # Clean up old history entries
    retention_days = config.TABLE_GROWTH_RETENTION_DAYS
    cutoff_date = today - timezone.timedelta(days=retention_days)
    deleted_count, _ = models.DailyTableSizeHistory.objects.filter(
        date__lt=cutoff_date
    ).delete()
    if deleted_count:
        logger.info(
            "Deleted %d old table size history entries older than %s",
            deleted_count,
            cutoff_date,
        )


@shared_task(name="waldur_core.check_table_growth_alerts")
def check_table_growth_alerts():
    """
    Check for tables that have grown abnormally fast and send alerts.
    Compares current sizes against 7-day and 30-day historical data.
    """
    from waldur_core.core.utils import broadcast_mail

    if not config.TABLE_GROWTH_MONITORING_ENABLED:
        logger.info(
            "Table growth monitoring is disabled, skipping check_table_growth_alerts"
        )
        return

    today = timezone.now().date()
    week_ago = today - timezone.timedelta(days=7)
    month_ago = today - timezone.timedelta(days=30)

    weekly_threshold = config.TABLE_GROWTH_WEEKLY_THRESHOLD_PERCENT
    monthly_threshold = config.TABLE_GROWTH_MONTHLY_THRESHOLD_PERCENT

    # Get current table sizes
    current_sizes = {
        entry.table_name: entry
        for entry in models.DailyTableSizeHistory.objects.filter(date=today)
    }

    # Get week-ago sizes
    week_ago_sizes = {
        entry.table_name: entry
        for entry in models.DailyTableSizeHistory.objects.filter(date=week_ago)
    }

    # Get month-ago sizes
    month_ago_sizes = {
        entry.table_name: entry
        for entry in models.DailyTableSizeHistory.objects.filter(date=month_ago)
    }

    alerts = []

    for table_name, current in current_sizes.items():
        # Check weekly growth
        if table_name in week_ago_sizes:
            week_ago_entry = week_ago_sizes[table_name]
            if week_ago_entry.total_size > 0:
                weekly_growth = (
                    (current.total_size - week_ago_entry.total_size)
                    / week_ago_entry.total_size
                    * 100
                )
                if weekly_growth > weekly_threshold:
                    alerts.append(
                        {
                            "table_name": table_name,
                            "period": "weekly",
                            "growth_percent": round(weekly_growth, 1),
                            "threshold": weekly_threshold,
                            "old_size": week_ago_entry.total_size,
                            "current_size": current.total_size,
                            "old_rows": week_ago_entry.row_estimate,
                            "current_rows": current.row_estimate,
                        }
                    )

        # Check monthly growth
        if table_name in month_ago_sizes:
            month_ago_entry = month_ago_sizes[table_name]
            if month_ago_entry.total_size > 0:
                monthly_growth = (
                    (current.total_size - month_ago_entry.total_size)
                    / month_ago_entry.total_size
                    * 100
                )
                if monthly_growth > monthly_threshold:
                    alerts.append(
                        {
                            "table_name": table_name,
                            "period": "monthly",
                            "growth_percent": round(monthly_growth, 1),
                            "threshold": monthly_threshold,
                            "old_size": month_ago_entry.total_size,
                            "current_size": current.total_size,
                            "old_rows": month_ago_entry.row_estimate,
                            "current_rows": current.row_estimate,
                        }
                    )

    if alerts:
        logger.warning(
            "Table growth alerts triggered for %d table(s): %s",
            len(alerts),
            [a["table_name"] for a in alerts],
        )

        # Get staff/support users to notify
        from django.db.models import Q

        from waldur_core.core.models import User

        recipients = list(
            User.objects.filter(is_active=True, notifications_enabled=True)
            .filter(Q(is_staff=True) | Q(is_support=True))
            .values_list("email", flat=True)
            .distinct()
        )

        # Filter out empty emails
        recipients = [email for email in recipients if email]

        if recipients:
            context = {
                "alerts": alerts,
                "date": today,
                "site_name": config.SITE_NAME,
            }
            broadcast_mail(
                "core",
                "table_growth_alert",
                context,
                recipients,
            )
            logger.info(
                "Sent table growth alert notification to %d recipients",
                len(recipients),
            )
    else:
        logger.info("No table growth alerts triggered")
