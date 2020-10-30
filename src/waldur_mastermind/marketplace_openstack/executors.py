from waldur_core.core import executors as core_executors

from . import tasks


class RestoreTenantLimitsExecutor(core_executors.BaseExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.restore_tenant_limits.si(serialized_instance)
