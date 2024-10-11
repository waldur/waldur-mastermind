from celery import chain

from waldur_core.core.executors import CreateExecutor
from waldur_core.core.tasks import BackendMethodTask, StateTransitionTask
from waldur_core.core.utils import serialize_instance
from waldur_openstack.executors import (
    NetworkCreateExecutor,
    RouterCreateExecutor,
    SecurityGroupCreateExecutor,
    SubNetCreateExecutor,
)
from waldur_openstack.models import Tenant

from . import models


class MigrationExecutor(CreateExecutor):
    @classmethod
    def get_task_signature(
        cls, migration: models.Migration, serialized_migration, **kwargs
    ):
        dst_tenant: Tenant = migration.dst_resource.scope
        serialized_tenant = serialize_instance(dst_tenant)
        creation_tasks = [
            StateTransitionTask().si(
                serialized_migration,
                state_transition="begin_creating",
            ),
            BackendMethodTask().si(
                serialized_tenant,
                "create_tenant_safe",
                state_transition="begin_creating",
            ),
            BackendMethodTask().si(serialized_tenant, "add_admin_user_to_tenant"),
            BackendMethodTask().si(serialized_tenant, "create_tenant_user"),
            BackendMethodTask().si(
                serialized_tenant,
                "push_tenant_quotas",
                dst_tenant.quota_limits,
            ),
            BackendMethodTask().si(
                serialized_tenant,
                "sync_default_security_group",
            ),
        ]
        for network in dst_tenant.networks.all():
            creation_tasks.append(NetworkCreateExecutor.as_signature(network))
            for subnet in network.subnets.all():
                SubNetCreateExecutor.as_signature(subnet)
        for security_group in dst_tenant.security_groups.all():
            if security_group.name != "default":
                creation_tasks.append(
                    SecurityGroupCreateExecutor.as_signature(security_group)
                )
        for router in dst_tenant.routers.all():
            creation_tasks.append(RouterCreateExecutor.as_signature(router))
        creation_tasks += [
            BackendMethodTask().si(serialized_tenant, "pull_tenant_quotas"),
            BackendMethodTask().si(serialized_tenant, "pull_tenant_images"),
            BackendMethodTask().si(serialized_tenant, "pull_tenant_flavors"),
            BackendMethodTask().si(serialized_tenant, "pull_tenant_volume_types"),
            BackendMethodTask().si(
                serialized_tenant, "pull_tenant_instance_availability_zones"
            ),
            BackendMethodTask().si(
                serialized_tenant, "pull_tenant_volume_availability_zones"
            ),
            StateTransitionTask().si(serialized_tenant, state_transition="set_ok"),
        ]
        return chain(*creation_tasks)
