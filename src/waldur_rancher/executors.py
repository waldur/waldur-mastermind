from celery import chain

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_core.core.models import StateMixin

from . import tasks, models


class ClusterCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user):
        _tasks = [core_tasks.BackendMethodTask().si(
            serialized_instance,
            'create_cluster',
            state_transition='begin_creating')]
        _tasks += tasks.RequestNodeCreation().si(
            serialized_instance,
            user_id=user.id,
        )
        _tasks += [core_tasks.PollRuntimeStateTask().si(
            serialized_instance,
            backend_pull_method='check_cluster_creating',
            success_state=models.Cluster.RuntimeStates.ACTIVE,
            erred_state='error'
        )]
        return chain(*_tasks)


class ClusterDeleteExecutor(core_executors.ErrorExecutorMixin, core_executors.BaseExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user):
        if instance.node_set.count():
            return tasks.DeleteClusterNodesTask().si(
                serialized_instance,
                user_id=user.id,
            )
        else:
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                'delete_cluster',
                state_transition='begin_deleting')

    @classmethod
    def pre_apply(cls, instance, **kwargs):
        instance.schedule_deleting()
        instance.save(update_fields=['state'])

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        if instance.node_set.count():
            # Removal will be in handlers
            return
        else:
            return super(ClusterDeleteExecutor, cls).get_success_signature(instance, serialized_instance, **kwargs)


class ClusterUpdateExecutor(core_executors.UpdateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id and {'name'} & set(kwargs['updated_fields']):
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                'update_cluster',
                state_transition='begin_updating')
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance,
                state_transition='begin_updating'
            )


class NodeCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user):
        return chain(
            tasks.CreateNodeTask().si(
                serialized_instance,
                user_id=user.id,
            ),
            tasks.PollRuntimeStateNodeTask().si(serialized_instance)
        )

    @classmethod
    def pre_apply(cls, instance, **kwargs):
        instance.begin_creating()
        instance.save()


class NodeDeleteExecutor(core_executors.ErrorExecutorMixin, core_executors.BaseExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user):
        node = instance

        if node.instance:
            return tasks.DeleteNodeTask().si(
                serialized_instance,
                user_id=user.id,
            )
        else:
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                'delete_node')

    @classmethod
    def pre_apply(cls, instance, **kwargs):
        # We can start deleting a node even if it does not have the status OK or Erred,
        # because a virtual machine could already be created.
        instance.state = StateMixin.States.DELETING
        instance.save(update_fields=['state'])

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        node = instance

        if node.instance:
            # Removal will be in handlers
            return
        else:
            return core_tasks.DeletionTask().si(serialized_instance)


class ClusterPullExecutor(core_executors.ActionExecutor):

    @classmethod
    def get_task_signature(cls, cluster, serialized_cluster, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_cluster, 'pull_cluster', state_transition='begin_updating')


class NodeRetryExecutor(core_executors.ErrorExecutorMixin, core_executors.BaseExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user):
        node = instance

        if node.instance:
            return tasks.DeleteNodeTask().si(
                serialized_instance,
                user_id=user.id,
            )
            # In this case, retry node creating will be called in handlers.
        else:
            return tasks.RetryNodeTask().si(
                serialized_instance,
                user_id=user.id,
            )

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.StateTransitionTask().si(serialized_instance, state_transition='begin_updating')
