from __future__ import unicode_literals

from .log import event_logger


def log_openstack_package_creation(sender, instance, created=False, **kwargs):
    if not created:
        return

    event_logger.openstack_package.info(
        'OpenStack package {tenant_name} has been created.',
        event_type='openstack_package_created',
        event_context={
            'tenant': instance.tenant,
            'package_template_name': instance.template.name,
            'service_settings': instance.service_settings,
        })


def log_openstack_package_deletion(sender, instance, **kwargs):
    event_logger.openstack_package.info(
        'OpenStack package {tenant_name} has been deleted.',
        event_type='openstack_package_deleted',
        event_context={
            'tenant': instance.tenant,
            'package_template_name': instance.template.name,
            'service_settings': instance.service_settings,
        })
