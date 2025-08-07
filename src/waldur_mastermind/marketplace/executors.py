from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils

from . import enums, tasks


class TerminateResourceExecutor(core_executors.BaseExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user, **kwargs):
        serialized_user = core_utils.serialize_instance(user)
        return tasks.terminate_resource.si(serialized_instance, serialized_user)


class MarketplaceActionExecutor(core_executors.BaseExecutor):
    @classmethod
    def pre_apply(cls, instance, **kwargs):
        if instance.state == enums.ResourceStates.UPDATING:
            return
        instance.set_state_updating()
        instance.save()

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.StateTransitionTask().si(
            serialized_instance,
            state_transition="set_state_ok",
            action="",
            action_details={},
        )

    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.StateTransitionTask().si(
            serialized_instance,
            state_transition="set_state_erred",
            action="",
            action_details={},
        )
