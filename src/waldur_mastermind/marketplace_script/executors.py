from waldur_core.core import executors as core_executors

from . import tasks


class DryRunExecutor(
    core_executors.SuccessExecutorMixin,
    core_executors.ErrorExecutorMixin,
    core_executors.BaseExecutor,
):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.dry_run_executor.si(instance.id)
