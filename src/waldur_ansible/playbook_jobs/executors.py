from celery import chain

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_openstack.openstack_tenant import executors as openstack_executors
from waldur_openstack.openstack_tenant import models as openstack_models


class RunJobExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, job, serialized_job, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_job, 'run_job', state_transition='begin_creating')


class DeleteJobExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, job, serialized_job, **kwargs):
        deletion_tasks = [
            core_tasks.StateTransitionTask().si(serialized_job, state_transition='begin_deleting')
        ]
        for resource in job.get_related_resources():
            force = resource.state == openstack_models.Instance.States.ERRED
            deletion_tasks.append(openstack_executors.InstanceDeleteExecutor.as_signature(
                resource, force=force, delete_volumes=True))
        return chain(*deletion_tasks)
