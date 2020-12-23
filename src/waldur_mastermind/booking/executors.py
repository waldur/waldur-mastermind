from celery import chain

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_mastermind.google.models import GoogleCalendar

from . import tasks


class UpdateExecutor(
    core_executors.SuccessExecutorMixin,
    core_executors.ErrorExecutorMixin,
    core_executors.BaseExecutor,
):
    @classmethod
    def pre_apply(cls, instance, **kwargs):
        if instance.state != GoogleCalendar.States.CREATION_SCHEDULED:
            instance.schedule_updating()
        instance.save(update_fields=['state'])

    @classmethod
    def _get_state_change_task(cls, instance, serialized_instance):
        if instance.state == GoogleCalendar.States.CREATION_SCHEDULED:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition='begin_creating'
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition='begin_updating'
            )


class GoogleCalendarSyncExecutor(UpdateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            cls._get_state_change_task(instance, serialized_instance),
            tasks.sync_bookings_to_google_calendar.si(serialized_instance),
        )


class GoogleCalendarShareExecutor(UpdateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            cls._get_state_change_task(instance, serialized_instance),
            tasks.share_google_calendar.si(serialized_instance),
        )


class GoogleCalendarUnShareExecutor(UpdateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            cls._get_state_change_task(instance, serialized_instance),
            tasks.unshare_google_calendar.si(serialized_instance),
        )


class GoogleCalendarRenameExecutor(UpdateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            cls._get_state_change_task(instance, serialized_instance),
            tasks.rename_google_calendar.si(serialized_instance),
        )
