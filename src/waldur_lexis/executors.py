import logging

from celery import chain

from waldur_core.core import executors
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils

logger = logging.getLogger(__name__)


class SshKeyCreateExecutor(
    executors.SuccessExecutorMixin, executors.ErrorExecutorMixin, executors.BaseExecutor
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
            .si(
                serialized_instance,
                "init_cluster_script_directory",
            )
            .set(countdown=5 * 60, max_retries=10, default_retry_delay=2 * 60),
        )


class SshKeyDeleteExecutor(executors.BaseExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance):
        serialized_robot_account = core_utils.serialize_instance(instance.robot_account)
        return chain(
            core_tasks.BackendMethodTask().si(serialized_instance, "delete_ssh_key"),
            core_tasks.BackendMethodTask().si(
                serialized_instance, "delete_heappe_project"
            ),
            core_tasks.DeletionTask().si(serialized_instance),
            core_tasks.DeletionTask().si(serialized_robot_account),
        )
