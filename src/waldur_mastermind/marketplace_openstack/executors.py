from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.structure import executors as structure_executors
from waldur_core.structure import models as structure_models
from waldur_openstack.openstack import executors as openstack_executors

from . import tasks


class RestoreTenantLimitsExecutor(core_executors.BaseExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.restore_tenant_limits.si(serialized_instance)


class MarketplaceTenantCreateExecutor(core_executors.BaseExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        service_settings = structure_models.ServiceSettings.objects.get(scope=tenant)
        serialized_service_settings = core_utils.serialize_instance(service_settings)

        create_tenant = openstack_executors.TenantCreateExecutor.get_task_signature(
            tenant, serialized_tenant, **kwargs
        )
        set_tenant_ok = openstack_executors.TenantCreateExecutor.get_success_signature(
            tenant, serialized_tenant
        )

        create_service_settings = (
            structure_executors.ServiceSettingsCreateExecutor.get_task_signature(
                service_settings, serialized_service_settings
            )
        )

        return create_tenant | set_tenant_ok | create_service_settings

    @classmethod
    def get_success_signature(cls, tenant, serialized_tenant, **kwargs):
        """Get Celery signature of task that should be applied on successful execution."""
        service_settings = structure_models.ServiceSettings.objects.get(scope=tenant)
        serialized_service_settings = core_utils.serialize_instance(service_settings)
        return core_tasks.StateTransitionTask().si(
            serialized_service_settings, state_transition="set_ok"
        )

    @classmethod
    def get_failure_signature(cls, tenant, serialized_tenant, **kwargs):
        return core_tasks.ErrorStateTransitionTask().s(serialized_tenant)
