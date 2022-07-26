import logging

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks

logger = logging.getLogger(__name__)


class FlavorCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, flavor, serialized_flavor, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_flavor,
            'create_flavor',
            state_transition='begin_creating',
        )


class FlavorDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, flavor, serialized_flavor, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_flavor,
            'delete_flavor',
            state_transition='begin_deleting',
        )
