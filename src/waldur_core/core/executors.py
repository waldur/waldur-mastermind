import operator

from waldur_core.core import models, utils, tasks


class BaseExecutor(object):
    """ Base class for describing logical operation with backend.

    Executor describes celery signature or primitive of low-level tasks that
    should be executed to provide high-level operation.

    Executor should handle:
     - low-level tasks execution;
     - models state changes;
    """

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        """ Get Celery signature or chain that describes executor action.

        Each task should be subclass of LowLevelTask class.
        Celery Signature and Primitives:
         - http://docs.celeryproject.org/en/latest/userguide/canvas.html
        Examples:
         - to execute only one task - return Signature of necessary task: `task.si(serialized_instance)`
         - to execute several tasks - return Chain of tasks: `chain(t1.s(), t2.s())`
        Note! Celery chord and group is not supported.
        """
        raise NotImplementedError('Executor %s should implement method `get_task_signature`' % cls.__name__)

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        """ Get Celery signature of task that should be applied on successful execution. """
        return None

    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, **kwargs):
        """ Get Celery signature of task that should be applied on failed execution. """
        return None

    @classmethod
    def execute(cls, instance, async=True, countdown=2, is_heavy_task=False, **kwargs):
        """ Execute high level-operation """
        cls.pre_apply(instance, async=async, **kwargs)
        serialized_instance = utils.serialize_instance(instance)

        signature = cls.get_task_signature(instance, serialized_instance, **kwargs)
        link = cls.get_success_signature(instance, serialized_instance, **kwargs)
        link_error = cls.get_failure_signature(instance, serialized_instance, **kwargs)
        if async:
            return signature.apply_async(
                link=link,
                link_error=link_error,
                countdown=countdown,
                queue=is_heavy_task and 'heavy' or None
            )
        else:
            result = signature.apply()
            callback = link if not result.failed() else link_error
            if callback is not None:
                if not callback.immutable:
                    callback.args = (result.id,) + callback.args
                callback.apply()
            return result.get()  # wait until task is ready

    @classmethod
    def pre_apply(cls, instance, **kwargs):
        """ Perform synchronous actions before signature apply """
        pass

    @classmethod
    def as_signature(cls, instance, **kwargs):
        serialized_instance = utils.serialize_instance(instance)
        pre_apply = tasks.PreApplyExecutorTask().si(
            utils.serialize_class(cls), serialized_instance, **kwargs)
        main = cls.get_task_signature(instance, serialized_instance, **kwargs)
        link = cls.get_success_signature(instance, serialized_instance, **kwargs)
        link_error = cls.get_failure_signature(instance, serialized_instance, **kwargs)
        parts = [task for task in [pre_apply, main, link]
                 if not isinstance(task, tasks.EmptyTask) and task is not None]
        signature = reduce(operator.or_, parts)
        if link_error:
            signature = signature.on_error(link_error)
        return signature


class ExecutorException(Exception):
    pass


class ErrorExecutorMixin(object):
    """ Set object as erred on fail. """

    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.ErrorStateTransitionTask().s(serialized_instance)


class SuccessExecutorMixin(object):
    """ Set object as OK on success, cleanup action and its details. """

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.StateTransitionTask().si(
            serialized_instance, state_transition='set_ok', action='', action_details={})


class DeleteExecutorMixin(object):
    """ Delete object on success or if force flag is enabled """

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.DeletionTask().si(serialized_instance)

    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, force=False, **kwargs):
        if force:
            return tasks.DeletionTask().si(serialized_instance)
        else:
            return tasks.ErrorStateTransitionTask().s(serialized_instance)


class EmptyExecutor(BaseExecutor):

    @classmethod
    def get_task_signature(cls, *args, **kwargs):
        return tasks.EmptyTask().si()


class CreateExecutor(SuccessExecutorMixin, ErrorExecutorMixin, BaseExecutor):
    """ Default states transition for object creation.

     - mark object as OK on success creation;
     - mark object as erred on failed creation;
    """
    pass


class UpdateExecutor(SuccessExecutorMixin, ErrorExecutorMixin, BaseExecutor):
    """ Default states transition for object update.

     - schedule updating before update;
     - mark object as OK on success update;
     - mark object as erred on failed update;
    """

    @classmethod
    def pre_apply(cls, instance, **kwargs):
        instance.schedule_updating()
        instance.save(update_fields=['state'])

    @classmethod
    def execute(cls, instance, async=True, **kwargs):
        if 'updated_fields' not in kwargs:
            raise ExecutorException('updated_fields keyword argument should be defined for UpdateExecutor.')
        super(UpdateExecutor, cls).execute(instance, async=async, **kwargs)


class DeleteExecutor(DeleteExecutorMixin, BaseExecutor):
    """ Default states transition for object deletion.

     - schedule deleting before deletion;
     - delete object on success deletion;
     - mark object as erred on failed deletion;
    """

    @classmethod
    def pre_apply(cls, instance, **kwargs):
        instance.schedule_deleting()
        instance.save(update_fields=['state'])


class ActionExecutor(SuccessExecutorMixin, ErrorExecutorMixin, BaseExecutor):
    """ Default states transition for executing action with object.

     - schedule updating before action execution;
     - mark object as OK on success action execution;
     - mark object as erred on failed action execution;
    """
    # TODO: After refactoring field action should become mandatory for implementation
    action = ''

    @classmethod
    def get_action_details(cls, instance, **kwargs):
        """ Get detailed action description """
        return {}

    @classmethod
    def pre_apply(cls, instance, **kwargs):
        if instance.state == models.StateMixin.States.UPDATE_SCHEDULED:
            return
        instance.schedule_updating()
        instance.action = cls.action
        instance.action_details = cls.get_action_details(instance, **kwargs)
        instance.save()
