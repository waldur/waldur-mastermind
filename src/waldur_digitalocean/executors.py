from celery import chain

from waldur_core.core import executors
from waldur_core.core.tasks import StateTransitionTask
from waldur_core.core.models import RuntimeStateMixin
from waldur_core.core import utils as core_utils
from waldur_core.structure import executors as structure_executors

from . import tasks, models


class DropletCreateExecutor(executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, droplet, serialized_droplet, **kwargs):
        return chain(
            tasks.SafeBackendMethodTask().si(
                serialized_droplet, 'create_droplet',
                state_transition='begin_creating',
                runtime_state='provisioning',
                success_runtime_state=RuntimeStateMixin.RuntimeStates.ONLINE,
                **kwargs),
            tasks.WaitForActionComplete().s(serialized_droplet).set(countdown=30))


class DropletDeleteExecutor(executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, droplet, serialized_droplet, **kwargs):
        if droplet.backend_id:
            return tasks.SafeBackendMethodTask().si(
                serialized_droplet, 'destroy', state_transition='begin_deleting')
        else:
            return StateTransitionTask().si(serialized_droplet, state_transition='begin_deleting')


class DropletStopExecutor(executors.ActionExecutor):

    @classmethod
    def get_task_signature(cls, droplet, serialized_droplet, **kwargs):
        return chain(
            tasks.SafeBackendMethodTask().si(
                serialized_droplet, 'stop',
                state_transition='begin_updating',
                success_runtime_state=models.Droplet.RuntimeStates.OFFLINE),
            tasks.WaitForActionComplete().s(serialized_droplet).set(countdown=10))


class DropletStartExecutor(executors.ActionExecutor):

    @classmethod
    def get_task_signature(cls, droplet, serialized_droplet, **kwargs):
        return chain(
            tasks.SafeBackendMethodTask().si(
                serialized_droplet, 'start',
                state_transition='begin_updating',
                success_runtime_state=models.Droplet.RuntimeStates.ONLINE),
            tasks.WaitForActionComplete().s(serialized_droplet).set(countdown=10))


class DropletRestartExecutor(executors.ActionExecutor):

    @classmethod
    def get_task_signature(cls, droplet, serialized_droplet, **kwargs):
        return chain(
            tasks.SafeBackendMethodTask().si(
                serialized_droplet, 'restart',
                state_transition='begin_updating',
                success_runtime_state=models.Droplet.RuntimeStates.ONLINE),
            tasks.WaitForActionComplete().s(serialized_droplet).set(countdown=10))


class DropletResizeExecutor(executors.UpdateExecutor):

    @classmethod
    def get_task_signature(cls, droplet, serialized_droplet, **kwargs):
        size = kwargs.pop('size')
        disk = kwargs.pop('disk')
        return chain(
            tasks.SafeBackendMethodTask().si(
                serialized_droplet, 'resize',
                state_transition='begin_updating',
                runtime_state='resizing',
                success_runtime_state=models.Droplet.RuntimeStates.ONLINE,
                backend_size_id=size.backend_id,
                disk=disk),
            tasks.WaitForActionComplete().s(serialized_droplet).set(countdown=10),
            tasks.LogDropletResized().si(serialized_droplet, core_utils.serialize_instance(size)))


class DigitalOceanCleanupExecutor(structure_executors.BaseCleanupExecutor):
    executors = (
        (models.Droplet, DropletDeleteExecutor),
    )
