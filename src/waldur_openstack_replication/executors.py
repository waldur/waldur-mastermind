from celery import chain

from waldur_core.core.executors import CreateExecutor
from waldur_core.core.tasks import StateTransitionTask
from waldur_openstack.executors import get_tenant_create_tasks

from . import models


class MigrationExecutor(CreateExecutor):
    @classmethod
    def get_task_signature(
        cls, migration: models.Migration, serialized_migration, **kwargs
    ):
        creation_tasks = [
            StateTransitionTask().si(
                serialized_migration,
                state_transition="begin_creating",
            ),
            get_tenant_create_tasks(
                migration.dst_resource.scope,
                migration.mappings.get("skip_connection_extnet", False),
            ),
        ]
        return chain(*creation_tasks)
