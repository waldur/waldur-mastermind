from __future__ import unicode_literals

from waldur_core.core import tasks as core_tasks, utils as core_utils
from waldur_openstack.openstack import models as openstack_models

from .log import event_logger


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
            package.service_settings.error_message = 'Failed to create tenant: %s.' % package.tenant
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
        package.service_settings.options['external_network_id'] = package.tenant.external_network_id
        package.service_settings.options['internal_network_id'] = package.tenant.internal_network_id
        package.service_settings.save()


class LogOpenStackPackageChange(core_tasks.Task):

    def execute(self, tenant, event, new_package, old_package, service_settings, *args, **kwargs):
        service_settings = core_utils.deserialize_instance(service_settings)
        event_type = 'openstack_package_change_succeeded' if event == 'succeeded' else 'openstack_package_change_failed'

        event_logger.openstack_package.info(
            'Tenant package changing has %s. '
            'Old value: %s, new value: {package_template_name}' % (event, old_package),
            event_type=event_type,
            event_context={
                'tenant': tenant,
                'package_template_name': new_package,
                'service_settings': service_settings,
            })
