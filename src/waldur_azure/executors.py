from __future__ import unicode_literals

from celery import chain

from waldur_core.core import executors as core_executors, tasks as core_tasks
from waldur_core.structure import executors as structure_executors

from . import models


class VirtualMachineStartExecutor(core_executors.ActionExecutor):
    action = 'Start'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance, backend_method='start_vm', state_transition='begin_updating',
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method='pull_virtual_machine_runtime_state',
                success_state='running',
                erred_state='erred'
            ),
        )


class VirtualMachineStopExecutor(core_executors.ActionExecutor):
    action = 'Stop'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance, backend_method='stop_vm', state_transition='begin_updating',
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method='pull_virtual_machine_runtime_state',
                success_state='stopped',
                erred_state='error'
            ),
        )


class VirtualMachineRestartExecutor(core_executors.ActionExecutor):
    action = 'Restart'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance, backend_method='reboot_vm', state_transition='begin_updating',
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method='pull_virtual_machine_runtime_state',
                success_state='running',
                erred_state='error'
            ),
        )


class VirtualMachineCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method='provision_vm',
                state_transition='begin_creating',
                **kwargs
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method='pull_virtual_machine_runtime_state',
                success_state='running',
                erred_state='error',
            ).set(countdown=30),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method='pull_vm_info',
            ),
        )


class VirtualMachineDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return chain(
                core_tasks.BackendMethodTask().si(
                    serialized_instance, backend_method='destroy_vm', state_transition='begin_deleting'),
                core_tasks.PollBackendCheckTask().si(serialized_instance, 'is_vm_deleted'),
            )
        else:
            return core_tasks.StateTransitionTask().si(serialized_instance, state_transition='begin_deleting')


class AzureCleanupExecutor(structure_executors.BaseCleanupExecutor):
    executors = (
        (models.VirtualMachine, VirtualMachineDeleteExecutor),
    )
