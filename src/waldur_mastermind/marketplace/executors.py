from waldur_core.core import executors as core_executors
from waldur_core.core import utils as core_utils

from . import tasks


class TerminateResourceExecutor(core_executors.BaseExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user, **kwargs):
        serialized_user = core_utils.serialize_instance(user)
        return tasks.terminate_resource.si(serialized_instance, serialized_user)
