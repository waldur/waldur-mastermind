from celery import chain

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils

from . import models


def pull_datastores_for_resource(instance, task):
    """
    Schedule datastore synchronization after virtual machine or disk
    has been either created, updated or deleted.
    """

    if isinstance(instance, models.VirtualMachine):
        datastore = instance.datastore
    elif isinstance(instance, models.Disk):
        datastore = instance.vm.datastore
    else:
        datastore = None

    if not datastore:
        return task

    serialized_settings = core_utils.serialize_instance(instance.service_settings)
    return chain(task, core_tasks.IndependentBackendMethodTask().si(
        serialized_settings,
        'pull_datastores',
    ))


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
        task = core_tasks.BackendMethodTask().si(
            serialized_instance,
            'create_virtual_machine',
            state_transition='begin_creating'
        )
        return chain(
            pull_datastores_for_resource(instance, task),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_vm_ports',
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_virtual_machine',
            ),
        )


class VirtualMachineDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            task = core_tasks.BackendMethodTask().si(
                serialized_instance,
                'delete_virtual_machine',
                state_transition='begin_deleting')
            return pull_datastores_for_resource(instance, task)
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance,
                state_transition='begin_deleting'
            )


class VirtualMachineStartExecutor(core_executors.ActionExecutor):
    action = 'Start'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        _tasks = [
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'start_virtual_machine',
                state_transition='begin_updating'
            )
        ]
        if instance.guest_power_enabled:
            _tasks.append(
                core_tasks.BackendMethodTask().si(
                    serialized_instance,
                    'pull_virtual_machine',
                )
            )
            _tasks.append(
                core_tasks.PollBackendCheckTask().si(
                    serialized_instance,
                    'is_virtual_machine_running'
                )
            )
        _tasks.append(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_virtual_machine',
            )
        )
        return chain(_tasks)


class VirtualMachineStopExecutor(core_executors.ActionExecutor):
    action = 'Stop'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'stop_virtual_machine',
                state_transition='begin_updating'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_virtual_machine',
            ),
        )


class VirtualMachineResetExecutor(core_executors.ActionExecutor):
    action = 'Reset'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        _tasks = [
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'reset_virtual_machine',
                state_transition='begin_updating'
            )
        ]
        if instance.guest_power_enabled:
            _tasks.append(
                core_tasks.BackendMethodTask().si(
                    serialized_instance,
                    'pull_virtual_machine',
                )
            )
            _tasks.append(
                core_tasks.PollBackendCheckTask().si(
                    serialized_instance,
                    'is_virtual_machine_running'
                )
            )
        _tasks.append(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_virtual_machine',
            )
        )
        return chain(_tasks)


class VirtualMachineSuspendExecutor(core_executors.ActionExecutor):
    action = 'Suspend'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'suspend_virtual_machine',
                state_transition='begin_updating'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_virtual_machine',
            ),
        )


class VirtualMachineShutdownGuestExecutor(core_executors.ActionExecutor):
    action = 'Shutdown Guest'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'shutdown_guest',
                state_transition='begin_updating'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_virtual_machine',
            ),
            core_tasks.PollBackendCheckTask().si(
                serialized_instance,
                'is_virtual_machine_shutted_down'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_virtual_machine',
            ),
        )


class VirtualMachineRebootGuestExecutor(core_executors.ActionExecutor):
    action = 'Reboot Guest'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'reboot_guest',
                state_transition='begin_updating'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_virtual_machine',
            ),
            core_tasks.PollBackendCheckTask().si(
                serialized_instance,
                'is_virtual_machine_running'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_virtual_machine',
            ),
        )


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


class PortCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        task = core_tasks.BackendMethodTask().si(
            serialized_instance,
            'create_port',
            state_transition='begin_creating'
        )
        return chain(
            task,
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_port',
            )
        )


class PortPullExecutor(core_executors.ActionExecutor):
    action = 'Pull'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            'pull_port',
            state_transition='begin_updating'
        )


class PortDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                'delete_port',
                state_transition='begin_deleting'
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance,
                state_transition='begin_deleting'
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
        task = core_tasks.BackendMethodTask().si(
            serialized_instance,
            'create_disk',
            state_transition='begin_creating'
        )
        task = pull_datastores_for_resource(instance, task)
        return chain(
            task,
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'pull_disk',
            )
        )


class DiskDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            task = core_tasks.BackendMethodTask().si(
                serialized_instance,
                'delete_disk',
                state_transition='begin_deleting'
            )
            return pull_datastores_for_resource(instance, task)
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance,
                state_transition='begin_deleting'
            )


class DiskExtendExecutor(core_executors.ActionExecutor):
    action = 'Extend'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        task = core_tasks.BackendMethodTask().si(
            serialized_instance, 'extend_disk',
            state_transition='begin_updating')
        return pull_datastores_for_resource(instance, task)
