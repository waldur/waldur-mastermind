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
