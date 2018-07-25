from waldur_core.core import executors as core_executors, tasks as core_tasks
from waldur_core.structure import executors as structure_executors

from . import models


class AllocationCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, volume, serialized_allocation, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_allocation,
            'create_allocation',
            state_transition='begin_creating'
        )


class AllocationUpdateExecutor(core_executors.UpdateExecutor):

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_volume,
            'set_resource_limits',
            state_transition='begin_updating'
        )


class AllocationPullExecutor(core_executors.ActionExecutor):
    action = 'Pull'

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_volume, 'pull_allocation',
            state_transition='begin_updating')


class AllocationDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, volume, serialized_allocation, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_allocation,
            'delete_allocation',
            state_transition='begin_deleting'
        )


class SlurmCleanupExecutor(structure_executors.BaseCleanupExecutor):
    executors = (
        (models.Allocation, AllocationDeleteExecutor),
    )
