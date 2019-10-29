from celery import chain

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks

from . import tasks


class ClusterCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, nodes, user):
        _tasks = [core_tasks.BackendMethodTask().si(
            serialized_instance,
            'create_cluster',
            state_transition='begin_creating').set(countdown=30)]
        _tasks += cls.create_nodes(serialized_instance, nodes, user)
        return chain(*_tasks)

    @classmethod
    def create_nodes(cls, serialized_cluster, nodes, user):
        _tasks = []
        for node in nodes:
            _tasks.append(tasks.CreateNodeTask().si(
                serialized_cluster,
                node=node,
                user_id=user.id,
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
