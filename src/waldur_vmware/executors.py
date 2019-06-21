from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks


class VirtualMachineCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            'create_virtual_machine',
            state_transition='begin_creating'
        )


class VirtualMachineDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                'delete_virtual_machine',
                state_transition='begin_deleting')
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance,
                state_transition='begin_deleting'
            )
