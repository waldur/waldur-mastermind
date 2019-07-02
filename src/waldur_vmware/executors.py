from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks


class VirtualMachinePullExecutor(core_executors.ActionExecutor):
    action = 'Pull'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            'pull_virtual_machine',
            state_transition='begin_updating'
        )


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


class VirtualMachineStartExecutor(core_executors.ActionExecutor):
    action = 'Start'

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_volume, 'start_virtual_machine',
            state_transition='begin_updating')


class VirtualMachineStopExecutor(core_executors.ActionExecutor):
    action = 'Stop'

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_volume, 'stop_virtual_machine',
            state_transition='begin_updating')


class VirtualMachineResetExecutor(core_executors.ActionExecutor):
    action = 'Reset'

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_volume, 'reset_virtual_machine',
            state_transition='begin_updating')


class VirtualMachineSuspendExecutor(core_executors.ActionExecutor):
    action = 'Suspend'

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_volume, 'suspend_virtual_machine',
            state_transition='begin_updating')


class VirtualMachineUpdateExecutor(core_executors.UpdateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if {'cores', 'cores_per_socket', 'ram'} & set(kwargs['updated_fields']):
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                'update_virtual_machine',
                state_transition='begin_updating'
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance,
                state_transition='begin_updating'
            )


class DiskPullExecutor(core_executors.ActionExecutor):
    action = 'Pull'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            'pull_disk',
            state_transition='begin_updating'
        )


class DiskCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            'create_disk',
            state_transition='begin_creating'
        )


class DiskDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                'delete_disk',
                state_transition='begin_deleting'
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance,
                state_transition='begin_deleting'
            )


class DiskExtendExecutor(core_executors.ActionExecutor):
    action = 'Extend'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance, 'extend_disk',
            state_transition='begin_updating')
