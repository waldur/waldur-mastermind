from celery import chain
from django.db.models import QuerySet

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import User
from waldur_rancher.enums import AGENT_ROLE, SERVER_ROLE

from . import models, tasks


class ClusterCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(
        cls,
        instance,
        serialized_instance,
        user,
        install_longhorn,
    ):
        _tasks = [
            core_tasks.BackendMethodTask().si(
                serialized_instance, "create_cluster", state_transition="begin_creating"
            )
        ]
        if instance.service_settings.get_option("vault_host"):
            _tasks += [
                tasks.CreateVaultCredentialsTask().si(
                    serialized_instance,
                )
            ]
        _tasks += cls.create_nodes(instance.node_set.all(), user)
        _tasks += [
            core_tasks.PollRuntimeStateTask()
            .si(
                serialized_instance,
                backend_pull_method="check_cluster_nodes",
                success_state=models.Cluster.RuntimeStates.ACTIVE,
                erred_state="error",
            )
            .set(countdown=120)
        ]
        _tasks += [
            # NB: countdown is needed for synchronization: wait until cluster will get ready for usage
            core_tasks.BackendMethodTask()
            .si(
                serialized_instance,
                "pull_cluster",
            )
            .set(countdown=120, max_retries=10, default_retry_delay=1 * 60)
        ]
        if instance.service_settings.get_option("argocd_k8s_kubeconfig"):
            _tasks += [
                tasks.CreateArgoCDClusterSecretTask().si(
                    serialized_instance,
                    install_longhorn=install_longhorn,
                )
            ]
        if instance.service_settings.get_option("vault_host"):
            _tasks += [tasks.DeleteVaultObjectsTask().si(serialized_instance)]
        return chain(*_tasks)

    @classmethod
    def create_nodes(cls, nodes: QuerySet[models.Node], user: User):
        _tasks = []
        # Schedule all the nodes to be created in parallel
        # TODO: need to validate once controlled deployment is working
        server_nodes = nodes.filter(role=SERVER_ROLE)
        agent_nodes = nodes.filter(role=AGENT_ROLE)

        # Create the server nodes in parallel
        for node in server_nodes:
            serialized_instance = core_utils.serialize_instance(node)
            _tasks.append(
                tasks.CreateNodeTask().si(
                    serialized_instance,
                    user_id=user.id,
                )
            )

        # Create one agent node
        first_agent_node = agent_nodes.first()
        serialized_first_node = core_utils.serialize_instance(first_agent_node)
        _tasks.append(
            tasks.CreateNodeTask().si(
                serialized_first_node,
                user_id=user.id,
            )
        )

        # Poll the runtime state of the server nodes
        for node in server_nodes:
            serialized_instance = core_utils.serialize_instance(node)
            _tasks += [
                tasks.PollRuntimeStateNodeTask().si(serialized_instance),
                core_tasks.StateTransitionTask().si(
                    serialized_instance, state_transition="set_ok"
                ),
            ]

        # Poll the runtime state of the first agent node
        _tasks += [
            tasks.PollRuntimeStateNodeTask().si(
                serialized_first_node,
            ),
            core_tasks.StateTransitionTask().si(
                serialized_first_node, state_transition="set_ok"
            ),
        ]

        # Create the rest of the agent nodes in parallel
        remaining_agent_nodes = agent_nodes.exclude(id=first_agent_node.id)

        for node in remaining_agent_nodes:
            serialized_instance = core_utils.serialize_instance(node)
            _tasks.append(
                tasks.CreateNodeTask().si(
                    serialized_instance,
                    user_id=user.id,
                )
            )
        # Poll the runtime state of the rest of the agent nodes
        for node in remaining_agent_nodes:
            serialized_instance = core_utils.serialize_instance(node)
            _tasks += [
                tasks.PollRuntimeStateNodeTask().si(serialized_instance),
                core_tasks.StateTransitionTask().si(
                    serialized_instance, state_transition="set_ok"
                ),
            ]

        return _tasks


class ClusterDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        # deletion of Cluster object is performed in handlers.py
        return None

    @classmethod
    def get_task_signature(
        cls, instance: models.Cluster, serialized_instance, user: User
    ):
        if instance.node_set.count():
            instance.begin_deleting()
            instance.save()
            _tasks = []

            for node in instance.node_set.all():
                _tasks.append(NodeDeleteExecutor.as_signature(node, user_id=user.id))

            _tasks.append(tasks.DeleteKeycloakGroupsTask().si(serialized_instance))
            if instance.service_settings.get_option("vault_host"):
                _tasks.append(tasks.DeleteVaultObjectsTask().si(serialized_instance))

            return chain(*_tasks)
        else:
            return core_tasks.BackendMethodTask().si(
                serialized_instance, "delete_cluster", state_transition="begin_deleting"
            )


class ClusterUpdateExecutor(core_executors.UpdateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id and {"name"} & set(kwargs["updated_fields"]):
            return core_tasks.BackendMethodTask().si(
                serialized_instance, "update_cluster", state_transition="begin_updating"
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition="begin_updating"
            )


class NodeCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(
        cls,
        instance,
        serialized_instance,
        user_id,
    ):
        return chain(
            tasks.CreateNodeTask().si(
                serialized_instance,
                user_id=user_id,
            ),
            tasks.PollRuntimeStateNodeTask().si(serialized_instance),
        )


class NodeDeleteExecutor(core_executors.BaseExecutor):
    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.ErrorStateTransitionTask().s(serialized_instance)

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user_id):
        return tasks.DeleteNodeTask().si(
            serialized_instance,
            user_id=user_id,
        )

    @classmethod
    def pre_apply(cls, instance, **kwargs):
        """
        We can start deleting a node even if it does not have the status OK or Erred,
        because a virtual machine could already be created.
        """
        instance.state = CoreStates.DELETION_SCHEDULED
        instance.save(update_fields=["state"])


class ClusterPullExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, cluster, serialized_cluster, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_cluster, "pull_cluster", state_transition="begin_updating"
            ),
        )


class NodePullExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, node, serialized_node, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_node, "pull_node", state_transition="begin_updating"
        )


class HPACreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance):
        return core_tasks.BackendMethodTask().si(
            serialized_instance, "create_hpa", state_transition="begin_creating"
        )


class HPAUpdateExecutor(core_executors.UpdateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance, "update_hpa", state_transition="begin_updating"
        )


class HPADeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_instance, "delete_hpa", state_transition="begin_deleting"
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition="begin_deleting"
            )


class ApplicationCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance, "create_app", state_transition="begin_creating"
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method="check_application_state",
                success_state="active",
                erred_state="error",
            ),
            core_tasks.BackendMethodTask().si(
                core_utils.serialize_instance(instance.rancher_project),
                "pull_project_workloads",
            ),
        )


class ApplicationDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_instance, "delete_app", state_transition="begin_deleting"
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition="begin_deleting"
            )
