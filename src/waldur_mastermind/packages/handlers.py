from django.conf import settings
from django.utils import timezone

from waldur_mastermind.invoices import registrators

from . import models
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
    if not settings.WALDUR_PACKAGES['BILLING_ENABLED']:
        return

    if created and instance.tenant.backend_id:
        registrators.RegistrationManager.register(instance, timezone.now())


def update_invoice_on_openstack_package_deletion(sender, instance, **kwargs):
    if not settings.WALDUR_PACKAGES['BILLING_ENABLED']:
        return

    registrators.RegistrationManager.terminate(instance, timezone.now())


def add_new_openstack_tenant_to_invoice(sender, instance, created=False, **kwargs):
    if not settings.WALDUR_PACKAGES['BILLING_ENABLED']:
        return

    if instance.backend_id and (created or not instance.tracker.previous('backend_id')):
        package = models.OpenStackPackage.objects.filter(tenant=instance).first()
        if package:
            registrators.RegistrationManager.register(package, timezone.now())
