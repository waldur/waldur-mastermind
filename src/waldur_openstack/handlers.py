import logging

from waldur_core.core import models as core_models
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.core.models import StateMixin
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_openstack import models

from .log import event_logger

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


def remove_ssh_key_from_all_tenants_on_it_deletion(sender, instance, **kwargs):
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


def log_tenant_quota_update(sender, instance, created=False, **kwargs):
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
    event_logger.openstack_tenant_quota.info(
        f"{{quota_name}} quota limit has been changed from {old_value_representation} to {new_value_representation} for tenant {{tenant_name}}.",
        event_type="openstack_tenant_quota_limit_updated",
        event_context={
            "quota_name": quota.name,
            "tenant": tenant,
            "limit": quota.value,
            "old_limit": quota.tracker.previous("value"),
        },
    )


def log_security_group_cleaned(sender, instance, **kwargs):
    event_logger.openstack_security_group.info(
        "Security group %s has been cleaned from cache." % instance.name,
        event_type="openstack_security_group_cleaned",
        event_context={
            "security_group": instance,
        },
    )


def log_security_group_rule_cleaned(sender, instance, **kwargs):
    event_logger.openstack_security_group_rule.info(
        "Security group rule %s has been cleaned from cache." % str(instance),
        event_type="openstack_security_group_rule_cleaned",
        event_context={
            "security_group_rule": instance,
        },
    )


def log_network_cleaned(sender, instance, **kwargs):
    event_logger.openstack_network.info(
        "Network %s has been cleaned from cache." % instance.name,
        event_type="openstack_network_cleaned",
        event_context={
            "network": instance,
        },
    )


def log_subnet_cleaned(sender, instance, **kwargs):
    event_logger.openstack_subnet.info(
        "SubNet %s has been cleaned." % instance.name,
        event_type="openstack_subnet_cleaned",
        event_context={
            "subnet": instance,
        },
    )


def log_server_group_cleaned(sender, instance, **kwargs):
    event_logger.openstack_server_group.info(
        "Server group %s has been cleaned from cache." % instance.name,
        event_type="openstack_server_group_cleaned",
        event_context={
            "server_group": instance,
        },
    )


def _log_scheduled_action(resource, action, action_details):
    class_name = resource.__class__.__name__.lower()
    message = _get_action_message(action, action_details)
    event_logger.openstack_resource_action.info(
        f'Operation "{message}" has been scheduled for {class_name} "{resource.name}"',
        event_type=_get_action_event_type(action, "scheduled"),
        event_context={"resource": resource, "action_details": action_details},
    )


def _log_succeeded_action(resource, action, action_details):
    if not action:
        return
    class_name = resource.__class__.__name__.lower()
    message = _get_action_message(action, action_details)
    event_logger.openstack_resource_action.info(
        f'Successfully executed "{message}" operation for {class_name} "{resource.name}"',
        event_type=_get_action_event_type(action, "succeeded"),
        event_context={"resource": resource, "action_details": action_details},
    )


def _log_failed_action(resource, action, action_details):
    class_name = resource.__class__.__name__.lower()
    message = _get_action_message(action, action_details)
    event_logger.openstack_resource_action.warning(
        f'Failed to execute "{message}" operation for {class_name} "{resource.name}"',
        event_type=_get_action_event_type(action, "failed"),
        event_context={"resource": resource, "action_details": action_details},
    )


def _get_action_message(action, action_details):
    return action_details.pop("message", action)


def _get_action_event_type(action, event_state):
    return "resource_{}_{}".format(action.replace(" ", "_").lower(), event_state)


def log_action(sender, instance, created=False, **kwargs):
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
    if resource.state == StateMixin.States.UPDATE_SCHEDULED:
        _log_scheduled_action(resource, resource.action, resource.action_details)
    if resource.state == StateMixin.States.OK:
        _log_succeeded_action(
            resource,
            resource.tracker.previous("action"),
            resource.tracker.previous("action_details"),
        )
    elif resource.state == StateMixin.States.ERRED:
        _log_failed_action(
            resource,
            resource.tracker.previous("action"),
            resource.tracker.previous("action_details"),
        )


def log_snapshot_schedule_creation(sender, instance, created=False, **kwargs):
    if not created:
        return

    snapshot_schedule = instance
    event_logger.openstack_snapshot_schedule.info(
        'Snapshot schedule "%s" has been created' % snapshot_schedule.name,
        event_type="resource_snapshot_schedule_created",
        event_context={
            "resource": snapshot_schedule.source_volume,
            "snapshot_schedule": snapshot_schedule,
        },
    )


def log_snapshot_schedule_action(sender, instance, created=False, **kwargs):
    snapshot_schedule = instance
    if created or not snapshot_schedule.tracker.has_changed("is_active"):
        return

    context = {
        "resource": snapshot_schedule.source_volume,
        "snapshot_schedule": snapshot_schedule,
    }
    if snapshot_schedule.is_active:
        event_logger.openstack_snapshot_schedule.info(
            'Snapshot schedule "%s" has been activated' % snapshot_schedule.name,
            event_type="resource_snapshot_schedule_activated",
            event_context=context,
        )
    else:
        if snapshot_schedule.error_message:
            message = f'Snapshot schedule "{snapshot_schedule.name}" has been deactivated because of error: {snapshot_schedule.error_message}'
        else:
            message = (
                'Snapshot schedule "%s" has been deactivated' % snapshot_schedule.name
            )
        event_logger.openstack_snapshot_schedule.warning(
            message,
            event_type="resource_snapshot_schedule_deactivated",
            event_context=context,
        )


def log_snapshot_schedule_deletion(sender, instance, **kwargs):
    snapshot_schedule = instance
    event_logger.openstack_snapshot_schedule.info(
        'Snapshot schedule "%s" has been deleted' % snapshot_schedule.name,
        event_type="resource_snapshot_schedule_deleted",
        event_context={
            "resource": snapshot_schedule.source_volume,
            "snapshot_schedule": snapshot_schedule,
        },
    )


def log_backup_schedule_creation(sender, instance, created=False, **kwargs):
    if not created:
        return

    backup_schedule = instance
    event_logger.openstack_backup_schedule.info(
        'Backup schedule "%s" has been created' % backup_schedule.name,
        event_type="resource_backup_schedule_created",
        event_context={
            "resource": backup_schedule.instance,
            "backup_schedule": backup_schedule,
        },
    )


def log_backup_schedule_action(sender, instance, created=False, **kwargs):
    backup_schedule = instance
    if created or not backup_schedule.tracker.has_changed("is_active"):
        return

    context = {"resource": backup_schedule.instance, "backup_schedule": backup_schedule}
    if backup_schedule.is_active:
        event_logger.openstack_backup_schedule.info(
            'Backup schedule "%s" has been activated' % backup_schedule.name,
            event_type="resource_backup_schedule_activated",
            event_context=context,
        )
    else:
        if backup_schedule.error_message:
            message = f'Backup schedule "{backup_schedule.name}" has been deactivated because of error: {backup_schedule.error_message}'
        else:
            message = 'Backup schedule "%s" has been deactivated' % backup_schedule.name
        event_logger.openstack_backup_schedule.warning(
            message,
            event_type="resource_backup_schedule_deactivated",
            event_context=context,
        )


def log_backup_schedule_deletion(sender, instance, **kwargs):
    backup_schedule = instance
    event_logger.openstack_backup_schedule.info(
        'Backup schedule "%s" has been deleted' % backup_schedule.name,
        event_type="resource_backup_schedule_deleted",
        event_context={
            "resource": backup_schedule.instance,
            "backup_schedule": backup_schedule,
        },
    )
