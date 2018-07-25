from waldur_core.core import executors as core_executors, tasks as core_tasks


class JupyterHubManagementRequestExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, jupyter_hub_management, serialized_jupyter_hub_management_request, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_jupyter_hub_management_request, 'process_jupyter_hub_management_request', state_transition='begin_creating')
