from waldur_core.logging.loggers import EventLogger, event_logger


class OpenStackPackageLogger(EventLogger):
    tenant = 'openstack.Tenant'
    service_settings = 'structure.ServiceSettings'
    package_template_name = basestring

    class Meta:
        event_types = ('openstack_package_created', 'openstack_package_deleted')
        event_groups = {
            'customers': event_types,
            'packages': event_types,
            'debug_only': event_types
        }
        nullable_fields = ('service_settings',)


event_logger.register('openstack_package', OpenStackPackageLogger)
