import logging

from celery import chain

from waldur_core.core import executors
from waldur_core.core import tasks as core_tasks

logger = logging.getLogger(__name__)


class SshKeyCreateExecutor(
    executors.SuccessExecutorMixin,  # Handle LexisLink state transition
    executors.ErrorExecutorMixin,
    executors.BaseExecutor,
):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance, "get_or_create_heappe_project"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance, "connect_heappe_project_to_cluster"
            ),
            core_tasks.BackendMethodTask().si(serialized_instance, "create_ssh_key"),
            core_tasks.BackendMethodTask()
            .si(serialized_instance, "test_user_access_to_project")
            .set(countdown=1 * 60, max_retries=10, default_retry_delay=1 * 60),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                "init_cluster_script_directory",
            ),
        )


class SshKeyDeleteExecutor(executors.BaseExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance):
        return chain(
            core_tasks.BackendMethodTask().si(serialized_instance, "delete_ssh_key"),
            core_tasks.BackendMethodTask().si(
                serialized_instance, "delete_heappe_project"
            ),
            core_tasks.DeletionTask().si(serialized_instance),
        )
