from celery import chain

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_mastermind.marketplace_rancher import const
from waldur_rancher import tasks as rancher_tasks


class ManagedRancherImportExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        _tasks = [
            core_tasks.BackendMethodTask().si(serialized_instance, "pull_cluster"),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method="import_cluster_public_ips",
                name_prefix=const.OS_LB_PREFIX,
            ),
        ]
        if instance.service_settings.get_option("argocd_k8s_kubeconfig"):
            _tasks.append(
                rancher_tasks.CreateArgoCDClusterSecretTask().si(serialized_instance)
            )

        return chain(*_tasks)
