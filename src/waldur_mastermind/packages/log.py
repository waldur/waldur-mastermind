from waldur_core.logging.loggers import EventLogger, event_logger


class OpenStackPackageLogger(EventLogger):
    tenant = 'openstack.Tenant'
    service_settings = 'structure.ServiceSettings'
    package_template_name = str

    class Meta:
        event_types = (
            'openstack_package_created',
            'openstack_package_change_scheduled',
            'openstack_package_change_succeeded',
            'openstack_package_change_failed',
            'openstack_package_deleted')
        event_groups = {
            'customers': event_types,
            'packages': event_types,
            'debug_only': event_types
        }
        nullable_fields = ('service_settings',)

    @staticmethod
    def get_scopes(event_context):
        tenant = event_context['tenant']
        project = tenant.service_project_link.project
        return {tenant, project, project.customer}


event_logger.register('openstack_package', OpenStackPackageLogger)
