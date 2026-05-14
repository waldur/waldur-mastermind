import logging

from django.contrib.contenttypes.models import ContentType

from waldur_core.core import models as core_models
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import SshPublicKey
from waldur_core.core.resolvers import register_resolver
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.quotas.models import QuotaLimit
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_openstack import models

logger = logging.getLogger(__name__)


def remove_ssh_key_from_tenants(sender, instance, **kwargs):
    """Delete user ssh keys from tenants that he does not have access now."""
    tenants = models.Tenant.objects.all()
    if isinstance(instance.scope, structure_models.Customer):
        tenants = tenants.filter(project__customer=instance.scope)
    elif isinstance(instance.scope, structure_models.Project):
        tenants = tenants.filter(project=instance.scope)
    else:
        return
    ssh_keys = core_models.SshPublicKey.objects.filter(user=instance.user)
    for tenant in tenants:
        if structure_permissions._has_admin_access(instance.user, tenant.project):
            continue  # no need to delete ssh keys if user still have permissions for tenant.
        serialized_tenant = core_utils.serialize_instance(tenant)
        key: core_models.SshPublicKey
        for key in ssh_keys:
            core_tasks.BackendMethodTask().delay(
                serialized_tenant,
                "remove_ssh_key_from_tenant",
                key.name,
                key.fingerprint_md5,
            )


def remove_ssh_key_from_all_tenants_on_it_deletion(
    sender, instance: SshPublicKey, **kwargs
):
    """Delete key from all tenants that are accessible for user on key deletion."""
    ssh_key: core_models.SshPublicKey = instance
    user = ssh_key.user
    tenants = structure_filters.filter_queryset_for_user(
        models.Tenant.objects.all(), user
    )
    for tenant in tenants:
        if not structure_permissions._has_admin_access(user, tenant.project):
            continue
        serialized_tenant = core_utils.serialize_instance(tenant)
        core_tasks.BackendMethodTask().delay(
            serialized_tenant,
            "remove_ssh_key_from_tenant",
            ssh_key.name,
            ssh_key.fingerprint_md5,
        )


def log_tenant_quota_update(sender, instance: QuotaLimit, created=False, **kwargs):
    """Log tenant quota updates."""
    quota = instance
    if created or not isinstance(quota.scope, models.Tenant):
        return

    if not quota.tracker.has_changed("value"):
        return

    tenant = quota.scope
    new_value_representation = quota.scope.format_quota(quota.name, quota.value)
    old_value_representation = quota.scope.format_quota(
        quota.name, quota.tracker.previous("value")
    )
    event_logger.emit(
        f"{{quota_name}} quota limit has been changed from {old_value_representation} to {new_value_representation} for tenant {{tenant_name}}.",
        event_type=EventType.OPENSTACK_TENANT_QUOTA_LIMIT_UPDATED,
        event_context={
            "quota_name": quota.name,
            "tenant": tenant,
            "limit": quota.value,
            "old_limit": quota.tracker.previous("value"),
        },
        scopes=[tenant, tenant.project, tenant.project.customer],
    )


def log_security_group_cleaned(sender, instance: models.SecurityGroup, **kwargs):
    """Log security group cleanup."""
    event_logger.emit(
        "Security group %s has been cleaned from cache." % instance.name,
        event_type=EventType.OPENSTACK_SECURITY_GROUP_CLEANED,
        event_context={
            "security_group": instance,
        },
        scopes=[instance, instance.tenant],
    )


def log_security_group_rule_cleaned(
    sender, instance: models.SecurityGroupRule, **kwargs
):
    """Per-rule cleanup events are intentionally not emitted.

    The aggregate openstack_security_group_rules_changed event (fired at the
    API or pull layer) covers the underlying change.
    """
    pass


def log_network_cleaned(sender, instance: models.Network, **kwargs):
    """Log network cleanup."""
    event_logger.emit(
        "Network %s has been cleaned from cache." % instance.name,
        event_type=EventType.OPENSTACK_NETWORK_CLEANED,
        event_context={
            "network": instance,
        },
        scopes=[instance, instance.tenant],
    )


def log_subnet_cleaned(sender, instance: models.SubNet, **kwargs):
    """Log subnet cleanup."""
    event_logger.emit(
        "SubNet %s has been cleaned." % instance.name,
        event_type=EventType.OPENSTACK_SUBNET_CLEANED,
        event_context={
            "subnet": instance,
        },
        scopes=[instance, instance.network],
    )


def log_server_group_cleaned(sender, instance: models.ServerGroup, **kwargs):
    """Log server group cleanup."""
    event_logger.emit(
        "Server group %s has been cleaned from cache." % instance.name,
        event_type=EventType.OPENSTACK_SERVER_GROUP_CLEANED,
        event_context={
            "server_group": instance,
        },
        scopes=[instance, instance.tenant],
    )


def get_resource_scopes(resource):
    project = resource.project
    return {resource, project, project.customer}


def _log_scheduled_action(resource, action, action_details):
    class_name = resource.__class__.__name__.lower()
    message = _get_action_message(action, action_details)
    event_logger.emit(
        f'Operation "{message}" has been scheduled for {class_name} "{resource.name}"',
        event_type=_get_action_event_type(action, "scheduled"),
        event_context={"resource": resource, "action_details": action_details},
        scopes=get_resource_scopes(resource),
    )


def _log_succeeded_action(resource, action, action_details):
    if not action:
        return
    class_name = resource.__class__.__name__.lower()
    message = _get_action_message(action, action_details)
    event_logger.emit(
        f'Successfully executed "{message}" operation for {class_name} "{resource.name}"',
        event_type=_get_action_event_type(action, "succeeded"),
        event_context={"resource": resource, "action_details": action_details},
        scopes=get_resource_scopes(resource),
    )


def _log_failed_action(resource, action, action_details):
    class_name = resource.__class__.__name__.lower()
    message = _get_action_message(action, action_details)
    event_logger.emit(
        f'Failed to execute "{message}" operation for {class_name} "{resource.name}"',
        event_type=_get_action_event_type(action, "failed"),
        event_context={"resource": resource, "action_details": action_details},
        scopes=get_resource_scopes(resource),
    )


def _get_action_message(action, action_details):
    return action_details.pop("message", action)


def _get_action_event_type(action, event_state):
    return EventType(
        "resource_{}_{}".format(action.replace(" ", "_").lower(), event_state)
    )


def log_action(sender, instance: models.Instance, created=False, **kwargs):
    """Log any resource action.

    Example of logged volume extend action:
    {
        'event_type': 'volume_extend_succeeded',
        'message': 'Successfully executed "Extend volume from 1024 MB to 2048 MB" operation for volume "pavel-test"',
        'action_details': {'old_size': 1024, 'new_size': 2048}
    }
    """
    resource = instance
    if created or not resource.tracker.has_changed("action"):
        return
    if resource.state == CoreStates.UPDATE_SCHEDULED:
        _log_scheduled_action(resource, resource.action, resource.action_details)
    if resource.state == CoreStates.OK:
        _log_succeeded_action(
            resource,
            resource.tracker.previous("action"),
            resource.tracker.previous("action_details"),
        )
    elif resource.state == CoreStates.ERRED:
        _log_failed_action(
            resource,
            resource.tracker.previous("action"),
            resource.tracker.previous("action_details"),
        )


def delete_state_service_properties(sender, instance: models.Tenant, **kwargs):
    """Delete state service properties."""
    models.Image.objects.filter(tenants=None).delete()
    models.Flavor.objects.filter(tenants=None).delete()
    models.VolumeType.objects.filter(tenants=None).delete()


@register_resolver("resource_external_ip")
def find_instance_by_external_ip(ip_address: str) -> list[tuple[int, int]]:
    """
    Resolves OpenStack instances by their Floating (external) IP address.
    """
    instance_ids = set(
        models.FloatingIP.objects.filter(address=ip_address)
        .values_list("port__instance_id", flat=True)
        .distinct()
    )

    if not instance_ids:
        return []

    content_type = ContentType.objects.get_for_model(models.Instance)
    return [
        (content_type.id, instance_id) for instance_id in instance_ids if instance_id
    ]


@register_resolver("resource_internal_ip")
def find_instance_by_internal_ip(ip_address: str) -> list[tuple[int, int]]:
    """
    Resolves OpenStack instances by their Port (internal) fixed IP address.
    """
    instance_ids = set(
        models.Port.objects.filter(
            fixed_ips__contains=ip_address, instance_id__isnull=False
        )
        .values_list("instance_id", flat=True)
        .distinct()
    )

    if not instance_ids:
        return []

    content_type = ContentType.objects.get_for_model(models.Instance)
    return [
        (content_type.id, instance_id) for instance_id in instance_ids if instance_id
    ]
