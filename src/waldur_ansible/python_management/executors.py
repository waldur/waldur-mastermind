from waldur_core.core import executors as core_executors, tasks as core_tasks


class PythonManagementRequestExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, python_management, serialized_python_management_request, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_python_management_request, 'process_python_management_request', state_transition='begin_creating')
