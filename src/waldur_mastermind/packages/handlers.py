from __future__ import unicode_literals

from django.utils import timezone

from waldur_mastermind.invoices import registrators

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


def add_new_openstack_package_details_to_invoice(sender, instance, created=False, **kwargs):
    if not created:
        return

    registrators.RegistrationManager.register(instance, timezone.now())


def update_invoice_on_openstack_package_deletion(sender, instance, **kwargs):
    registrators.RegistrationManager.terminate(instance, timezone.now())
