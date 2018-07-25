from __future__ import unicode_literals

from celery import chain

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks


class VolumePullExecutor(core_executors.ActionExecutor):
    action = 'Pull'

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_volume, 'pull_volume',
            state_transition='begin_updating')


class VolumeCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_volume,
                'create_volume',
                state_transition='begin_creating'
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method='pull_volume_runtime_state',
                success_state='available',
                erred_state='error',
            ).set(countdown=30)
        )


class VolumeDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        if volume.backend_id:
            return chain(
                core_tasks.BackendMethodTask().si(
                    serialized_volume, 'delete_volume', state_transition='begin_deleting'),
                core_tasks.PollBackendCheckTask().si(serialized_volume, 'is_volume_deleted'),
            )
        else:
            return core_tasks.StateTransitionTask().si(serialized_volume, state_transition='begin_deleting')


class InstancePullExecutor(core_executors.ActionExecutor):
    action = 'Pull'

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_volume, 'pull_instance',
            state_transition='begin_updating')


class InstanceCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_volume,
            'create_instance',
            state_transition='begin_creating'
        )


class InstanceDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return cls.get_delete_instance_tasks(instance, serialized_instance)
        else:
            return core_tasks.StateTransitionTask().si(serialized_instance, state_transition='begin_deleting')

    @classmethod
    def get_delete_instance_tasks(cls, instance, serialized_instance):
        _tasks = [
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                'delete_instance',
                state_transition='begin_deleting'
            ),
            core_tasks.PollBackendCheckTask().si(
                serialized_instance,
                'is_instance_deleted'
            ),
            core_tasks.IndependentBackendMethodTask().si(
                serialized_instance,
                'pull_networks'
            ),
        ]

        if instance.floating_ip:
            _tasks.append(core_tasks.IndependentBackendMethodTask().si(
                serialized_instance,
                'pull_floating_ips'
            ))

        return chain(_tasks)
