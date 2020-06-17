from celery import chain

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_core.core.models import StateMixin

from . import models, tasks


class ClusterCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user, install_longhorn):
        _tasks = [
            core_tasks.BackendMethodTask().si(
                serialized_instance, 'create_cluster', state_transition='begin_creating'
            )
        ]
        _tasks += cls.create_nodes(instance.node_set.all(), user)
        _tasks += [
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method='check_cluster_nodes',
                success_state=models.Cluster.RuntimeStates.ACTIVE,
                erred_state='error',
            )
        ]
        _tasks += [
            core_tasks.BackendMethodTask().si(serialized_instance, 'pull_cluster',)
        ]
        if install_longhorn:
            _tasks += [
                core_tasks.BackendMethodTask().si(
                    serialized_instance, 'install_longhorn_to_cluster',
                )
            ]
        return chain(*_tasks)

    @classmethod
    def create_nodes(cls, nodes, user):
        _tasks = []
        # schedule first controlplane nodes so that Rancher would be able to register other nodes
        # TODO: need to assure that also etcd is registered - probably parallel Node creation can be a solution
        # TODO: need to validate once controlled deployment is working
        for node in nodes.order_by('-controlplane_role'):
            _tasks.append(NodeCreateExecutor.as_signature(node, user_id=user.id))
        return _tasks


class ClusterDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        # deletion of Cluster object is performed in handlers.py
        return None

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user):
        if instance.node_set.count():
            instance.begin_deleting()
            instance.save()
            _tasks = []

            for node in instance.node_set.all():
                _tasks.append(NodeDeleteExecutor.as_signature(node, user_id=user.id))

            return chain(*_tasks)
        else:
            return core_tasks.BackendMethodTask().si(
                serialized_instance, 'delete_cluster', state_transition='begin_deleting'
            )


class ClusterUpdateExecutor(core_executors.UpdateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id and {'name'} & set(kwargs['updated_fields']):
            return core_tasks.BackendMethodTask().si(
                serialized_instance, 'update_cluster', state_transition='begin_updating'
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition='begin_updating'
            )


class NodeCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user_id):
        return chain(
            tasks.CreateNodeTask().si(serialized_instance, user_id=user_id,),
            tasks.PollRuntimeStateNodeTask().si(serialized_instance),
        )


class NodeDeleteExecutor(core_executors.BaseExecutor):
    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.ErrorStateTransitionTask().s(serialized_instance)

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user_id):
        return tasks.DeleteNodeTask().si(serialized_instance, user_id=user_id,)

    @classmethod
    def pre_apply(cls, instance, **kwargs):
        """
        We can start deleting a node even if it does not have the status OK or Erred,
        because a virtual machine could already be created.
        """
        instance.state = StateMixin.States.DELETION_SCHEDULED
        instance.save(update_fields=['state'])


class ClusterPullExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, cluster, serialized_cluster, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_cluster, 'pull_cluster', state_transition='begin_updating'
            ),
        )


class NodePullExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, node, serialized_node, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_node, 'pull_node', state_transition='begin_updating'
        )


class HPADeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_instance, 'delete_hpa', state_transition='begin_deleting'
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition='begin_deleting'
            )
