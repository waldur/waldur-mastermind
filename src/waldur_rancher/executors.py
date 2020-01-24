from celery import chain

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks, utils as core_utils

from . import tasks, models


class PollNodeStateTask(core_tasks.Task):
    max_retries = 1200
    default_retry_delay = 5

    @classmethod
    def get_description(cls, instance, backend_pull_method, *args, **kwargs):
        return 'Poll node "%s" with method "%s"' % (instance, backend_pull_method)

    def get_backend(self, instance):
        return instance.get_backend()

    def execute(self, instance, backend_pull_method):
        backend = self.get_backend(instance)
        getattr(backend, backend_pull_method)(instance)
        instance.refresh_from_db()

        if not instance.runtime_state:
            self.retry()

        return instance


class ClusterCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, user):
        _tasks = [core_tasks.BackendMethodTask().si(
            serialized_instance,
            'create_cluster',
            state_transition='begin_creating')]
        _tasks += cls.create_nodes(instance.node_set.all(), user)
        return chain(*_tasks)

    @classmethod
    def create_nodes(cls, nodes, user):
        _tasks = []
        for node in nodes:
            serialized_instance = core_utils.serialize_instance(node)
            _tasks.append(tasks.CreateNodeTask().si(
                serialized_instance,
                user_id=user.id,
            ))

        for node in nodes:
            serialized_instance = core_utils.serialize_instance(node)
            _tasks.append(tasks.CreateNodeTask().si(
                serialized_instance,
                user_id=user.id,
            ))
            _tasks.append(PollNodeStateTask().si(
                serialized_instance,
                backend_pull_method='update_node_details',
            ))
        return _tasks


class ClusterDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                'delete_cluster',
                state_transition='begin_deleting')
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance,
                state_transition='begin_deleting'
            )


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
        return tasks.CreateNodeTask().si(
            serialized_instance,
            user_id=user.id,
        )


class ClusterPullExecutor(core_executors.ActionExecutor):

    @classmethod
    def get_task_signature(cls, cluster, serialized_cluster, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_cluster, 'pull_cluster', state_transition='begin_updating')
