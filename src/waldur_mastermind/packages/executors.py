from waldur_core.core import executors as core_executors, tasks as core_tasks, utils as core_utils
from waldur_core.structure import executors as structure_executors
from waldur_mastermind.packages.serializers import _get_template_quotas
from waldur_openstack.openstack import executors as openstack_executors

from . import tasks


class OpenStackPackageCreateExecutor(core_executors.BaseExecutor):

    @classmethod
    def get_task_signature(cls, package, serialized_package, **kwargs):
        tenant = package.tenant
        serialized_tenant = core_utils.serialize_instance(tenant)
        service_settings = package.service_settings
        serialized_service_settings = core_utils.serialize_instance(service_settings)

        create_tenant = openstack_executors.TenantCreateExecutor.get_task_signature(tenant, serialized_tenant, **kwargs)
        set_tenant_ok = openstack_executors.TenantCreateExecutor.get_success_signature(tenant, serialized_tenant)

        populate_service_settings = tasks.OpenStackPackageSettingsPopulationTask().si(serialized_package)

        create_service_settings = structure_executors.ServiceSettingsCreateExecutor.get_task_signature(
            service_settings, serialized_service_settings)

        return create_tenant | set_tenant_ok | populate_service_settings | create_service_settings

    @classmethod
    def get_success_signature(cls, package, serialized_package, **kwargs):
        """ Get Celery signature of task that should be applied on successful execution. """
        service_settings = package.service_settings
        serialized_service_settings = core_utils.serialize_instance(service_settings)
        return core_tasks.StateTransitionTask().si(serialized_service_settings, state_transition='set_ok')

    @classmethod
    def get_failure_signature(cls, package, serialized_package, **kwargs):
        return tasks.OpenStackPackageErrorTask().s(serialized_package)


class OpenStackPackageChangeExecutor(core_executors.BaseExecutor):

    @classmethod
    def get_success_signature(cls, tenant, serialized_tenant, new_template, old_package, service_settings, **kwargs):
        service_settings = core_utils.serialize_instance(service_settings)
        return tasks.LogOpenStackPackageChange().si(serialized_tenant, event='succeeded',
                                                    new_package=new_template.name,
                                                    old_package=old_package.template.name,
                                                    service_settings=service_settings)

    @classmethod
    def get_failure_signature(cls, tenant, serialized_tenant, new_template, old_package, service_settings, **kwargs):
        service_settings = core_utils.serialize_instance(service_settings)
        return tasks.LogOpenStackPackageChange().si(serialized_tenant, event='failed',
                                                    new_package=new_template.name,
                                                    old_package=old_package.template.name,
                                                    service_settings=service_settings)

    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, new_template, old_package, service_settings, **kwargs):
        quotas = {
            quota_field.name: value
            for quota_field, value in _get_template_quotas(new_template).items()
        }
        push_quotas = openstack_executors.TenantPushQuotasExecutor.as_signature(tenant, quotas=quotas)

        serialized_new_template = core_utils.serialize_instance(new_template)
        serialized_old_package = core_utils.serialize_instance(old_package)
        serialized_service_settings = core_utils.serialize_instance(service_settings)

        success_package_change = tasks.OpenStackPackageSuccessTask().si(serialized_tenant,
                                                                        serialized_new_template,
                                                                        serialized_old_package,
                                                                        serialized_service_settings)
        return push_quotas | success_package_change
