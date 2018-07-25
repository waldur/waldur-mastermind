from __future__ import unicode_literals

import logging


from waldur_core.core import models as core_models, tasks as core_tasks, utils as core_utils
from waldur_core.structure import (filters as structure_filters, permissions as structure_permissions,
                                   models as structure_models)
from waldur_openstack.openstack import apps

from .log import event_logger
from .models import Tenant


logger = logging.getLogger(__name__)


def remove_ssh_key_from_tenants(sender, structure, user, role, **kwargs):
    """ Delete user ssh keys from tenants that he does not have access now. """
    tenants = Tenant.objects.filter(**{sender.__name__.lower(): structure})
    ssh_keys = core_models.SshPublicKey.objects.filter(user=user)
    for tenant in tenants:
        if structure_permissions._has_admin_access(user, tenant.project):
            continue  # no need to delete ssh keys if user still have permissions for tenant.
        serialized_tenant = core_utils.serialize_instance(tenant)
        for key in ssh_keys:
            core_tasks.BackendMethodTask().delay(
                serialized_tenant, 'remove_ssh_key_from_tenant', key.name, key.fingerprint)


def remove_ssh_key_from_all_tenants_on_it_deletion(sender, instance, **kwargs):
    """ Delete key from all tenants that are accessible for user on key deletion. """
    ssh_key = instance
    user = ssh_key.user
    tenants = structure_filters.filter_queryset_for_user(Tenant.objects.all(), user)
    for tenant in tenants:
        if not structure_permissions._has_admin_access(user, tenant.project):
            continue
        serialized_tenant = core_utils.serialize_instance(tenant)
        core_tasks.BackendMethodTask().delay(
            serialized_tenant, 'remove_ssh_key_from_tenant', ssh_key.name, ssh_key.fingerprint)


def log_tenant_quota_update(sender, instance, created=False, **kwargs):
    quota = instance
    if created or not isinstance(quota.scope, Tenant):
        return

    if not quota.tracker.has_changed('limit'):
        return

    tenant = quota.scope
    new_value_representation = quota.scope.format_quota(quota.name, quota.limit)
    old_value_representation = quota.scope.format_quota(quota.name, quota.tracker.previous('limit'))
    event_logger.openstack_tenant_quota.info(
        '{quota_name} quota limit has been changed from %s to %s for tenant {tenant_name}.' %
        (old_value_representation, new_value_representation),
        event_type='openstack_tenant_quota_limit_updated',
        event_context={
            'quota': quota,
            'tenant': tenant,
            'limit': float(quota.limit),
            'old_limit': float(quota.tracker.previous('limit')),
        })


def update_service_settings_name(sender, instance, created=False, **kwargs):
    tenant = instance

    if created or not tenant.tracker.has_changed('name'):
        return

    try:
        service_settings = structure_models.ServiceSettings.objects.get(scope=tenant,
                                                                        type=apps.OpenStackConfig.service_name)
    except structure_models.ServiceSettings.DoesNotExist:
        return
    else:
        service_settings.name = tenant.name
        service_settings.save()
