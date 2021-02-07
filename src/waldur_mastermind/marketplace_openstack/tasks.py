from celery import shared_task
from django.db import transaction

from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace_openstack import serializers
from waldur_mastermind.packages import models
from waldur_openstack.openstack import models as openstack_models

from . import utils


@shared_task(name='waldur_mastermind.marketplace_openstack.push_tenant_limits')
def push_tenant_limits(serialized_resource):
    resource = core_utils.deserialize_instance(serialized_resource)
    utils.push_tenant_limits(resource)


@shared_task(name='waldur_mastermind.marketplace_openstack.restore_tenant_limits')
def restore_tenant_limits(serialized_resource):
    resource = core_utils.deserialize_instance(serialized_resource)
    utils.restore_limits(resource)


class OpenStackPackageErrorTask(core_tasks.ErrorStateTransitionTask):
    """ Handle OpenStack package creation errors.

        If error occurred on tenant creation - mark tenant and service settings as erred.
        If error occurred on service settings creation - mark only service settings as erred.
    """

    @classmethod
    def get_description(cls, result_id, package, *args, **kwargs):
        return 'Mark package "%s" components as erred.' % package

    def execute(self, package):
        if package.tenant.state != openstack_models.Tenant.States.OK:
            self.state_transition(package.tenant, 'set_erred')
            self.save_error_message(package.tenant)
            self.state_transition(package.service_settings, 'set_erred')
            package.service_settings.error_message = (
                'Failed to create tenant: %s.' % package.tenant
            )
            package.service_settings.save(update_fields=['error_message'])
        else:
            self.state_transition(package.service_settings, 'set_erred')
            self.save_error_message(package.service_settings)


class OpenStackPackageSettingsPopulationTask(core_tasks.Task):
    """ Populate service settings options based on provisioned tenant. """

    @classmethod
    def get_description(cls, package, *args, **kwargs):
        return 'Copy tenant backend data to settings. Package "%s".' % package

    def execute(self, package):
        package.service_settings.options['tenant_id'] = package.tenant.backend_id
        package.service_settings.options[
            'external_network_id'
        ] = package.tenant.external_network_id
        package.service_settings.options[
            'internal_network_id'
        ] = package.tenant.internal_network_id
        package.service_settings.save()


class OpenStackPackageSuccessTask(core_tasks.Task):
    def execute(
        self,
        tenant,
        serialized_new_template,
        serialized_old_package,
        serialized_service_settings,
        *args,
        **kwargs
    ):
        new_template = core_utils.deserialize_instance(serialized_new_template)
        old_package = core_utils.deserialize_instance(serialized_old_package)
        service_settings = core_utils.deserialize_instance(serialized_service_settings)

        with transaction.atomic():
            serializers._set_tenant_quotas(tenant, new_template)
            serializers._set_related_service_settings_quotas(tenant, new_template)
            serializers._set_tenant_extra_configuration(tenant, new_template)
            old_package.delete()
            models.OpenStackPackage.objects.create(
                template=new_template, service_settings=service_settings, tenant=tenant
            )
