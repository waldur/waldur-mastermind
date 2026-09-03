import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.structure import tasks as structure_tasks
from waldur_core.structure.registry import get_resource_type
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes
from waldur_mastermind.marketplace_openstack.utils import (
    create_offerings_for_volume_and_instance,
)
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackTenantNotFound

from . import models, signals

logger = logging.getLogger(__name__)

TERMINATION_STEP_STOP = "stop"
TERMINATION_STEP_DELETE_BACKUP = "delete_backup"
TERMINATION_STEP_DELETE_SNAPSHOT = "delete_snapshot"
TERMINATION_STEP_DETACH_VOLUMES = "detach_volumes"
TERMINATION_STEP_DELETE_INSTANCE = "delete_instance"


def get_instance_termination_log_context(instance):
    context = {
        "instance_uuid": instance.uuid.hex,
        "resource_uuid": None,
        "order_uuid": None,
    }
    try:
        resource = marketplace_models.Resource.objects.get(scope=instance)
        context["resource_uuid"] = resource.uuid.hex
        order = (
            marketplace_models.Order.objects.filter(
                resource=resource,
                type=OrderTypes.TERMINATE,
                state=OrderStates.EXECUTING,
            )
            .order_by("-created")
            .first()
        )
        if order:
            context["order_uuid"] = order.uuid.hex
    except marketplace_models.Resource.DoesNotExist:
        pass
    return context


def log_instance_termination_event(instance, event, step, context=None):
    context = context or get_instance_termination_log_context(instance)
    logger.log(
        logging.ERROR if event == "failed" else logging.INFO,
        "instance_termination event=%s step=%s instance_uuid=%s resource_uuid=%s "
        "order_uuid=%s",
        event,
        step,
        context["instance_uuid"],
        context["resource_uuid"],
        context["order_uuid"],
    )


class InstanceTerminationStepMarkerTask(core_tasks.Task):
    @classmethod
    def get_description(cls, instance, step, **kwargs):
        return f'Mark instance termination step "{step}" for instance "{instance}".'

    def execute(self, instance, step, **kwargs):
        action_details = dict(instance.action_details or {})
        action_details["termination_step"] = step
        instance.action_details = action_details
        instance.save(update_fields=["action_details"])
        log_instance_termination_event(instance, "started", step)


class InstanceDeleteSuccessTask(core_tasks.DeletionTask):
    def execute(self, instance):
        step = (instance.action_details or {}).get(
            "termination_step", TERMINATION_STEP_DELETE_INSTANCE
        )
        log_instance_termination_event(instance, "completed", step)
        super().execute(instance)


class InstanceDeleteFailureTask(core_tasks.ErrorStateTransitionTask):
    def execute(self, instance):
        step = (instance.action_details or {}).get("termination_step", "unknown")
        log_context = get_instance_termination_log_context(instance)
        super().execute(instance)
        if instance.error_message and not instance.error_message.startswith(
            "Termination failed at step"
        ):
            instance.error_message = (
                f"Termination failed at step '{step}': {instance.error_message}"
            )
            instance.save(update_fields=["error_message"])
        log_instance_termination_event(instance, "failed", step, context=log_context)


class TenantCreateErrorTask(core_tasks.ErrorStateTransitionTask):
    def execute(self, tenant):
        super().execute(tenant)
        # Delete network and subnet if they were not created on backend,
        # mark as erred if they were created
        network = tenant.networks.first()
        if network is None:
            return
        subnet = network.subnets.first()
        if subnet.state == CoreStates.CREATION_SCHEDULED:
            subnet.delete()
        else:
            super().execute(subnet)
        if network.state == CoreStates.CREATION_SCHEDULED:
            network.delete()
        else:
            super().execute(network)


class TenantCreateSuccessTask(core_tasks.StateTransitionTask):
    def execute(self, tenant):
        # Avoid triggering executors.TenantPullExecutor, which starts an asynchronous
        # sync task that sets the tenant to UPDATING state, causing race conditions
        # for clients (like Terraform) expecting the tenant to be immediately OK and ready.
        # Instead, trigger only the required post-creation side-effects directly.
        signals.tenant_pull_succeeded.send(models.Tenant, instance=tenant)
        create_offerings_for_volume_and_instance(tenant)
        return super().execute(tenant)


class TenantPullQuotas(core_tasks.BackgroundTask):
    """Pull quota limits and usage information for all OpenStack tenants."""

    name = "openstack.TenantPullQuotas"

    def run(self):
        from . import executors

        for tenant in models.Tenant.objects.filter(state=CoreStates.OK):
            executors.TenantPullQuotasExecutor.execute(tenant)


class SendSignalTenantPullSucceeded(core_tasks.Task):
    @classmethod
    def get_description(cls, *args, **kwargs):
        return "Send tenant_pull_succeeded signal."

    def execute(self, tenant):
        signals.tenant_pull_succeeded.send(models.Tenant, instance=tenant)


@shared_task(name="openstack.mark_as_erred_old_tenants_in_deleting_state")
def mark_as_erred_old_tenants_in_deleting_state():
    """Mark OpenStack tenants as erred if they have been in deleting state for more than 1 day."""
    models.Tenant.objects.filter(
        modified__lte=timezone.now() - timezone.timedelta(days=1),
        state=CoreStates.DELETING,
    ).update(
        state=CoreStates.ERRED,
        error_message="Deletion error. Deleting took more than a day.",
    )


@shared_task
def check_existence_of_tenant(serialized_tenant):
    tenant = core_utils.deserialize_instance(serialized_tenant)
    backend = tenant.get_backend()

    if backend.does_tenant_exist_in_backend(tenant) is False:
        raise OpenStackTenantNotFound(f"Tenant {tenant} does not exist in backend.")


@shared_task
def mark_tenant_as_deleted(serialized_tenant):
    tenant = core_utils.deserialize_instance(serialized_tenant)
    tenant.set_erred()
    tenant.save()

    signals.tenant_does_not_exist_in_backend.send(models.Tenant, instance=tenant)


class SetInstanceOKTask(core_tasks.StateTransitionTask):
    """Additionally mark or related floating IPs as free"""

    def pre_execute(self, instance):
        self.kwargs["state_transition"] = "set_ok"
        self.kwargs["action"] = ""
        self.kwargs["action_details"] = {}
        super().pre_execute(instance)

    def execute(self, instance, *args, **kwargs):
        super().execute(instance)
        instance.floating_ips.update(state=CoreStates.OK)


class SetInstanceErredTask(core_tasks.ErrorStateTransitionTask):
    """Mark instance as erred and delete resources that were not created."""

    def execute(self, instance):
        super().execute(instance)

        # delete volumes if they were not created on backend,
        # mark as erred if creation was started, but not ended,
        # leave as is, if they are OK.
        for volume in instance.volumes.all():
            if volume.state == CoreStates.CREATION_SCHEDULED:
                volume.delete()
            elif volume.state == CoreStates.OK:
                pass
            else:
                volume.set_erred()
                volume.save(update_fields=["state"])

        # set instance floating IPs as free, delete not created ones.
        instance.floating_ips.filter(backend_id="").delete()
        instance.floating_ips.update(state=CoreStates.OK)


class SetBackupErredTask(core_tasks.ErrorStateTransitionTask):
    """Mark DR backup and all related resources that are not in state OK as Erred"""

    def execute(self, backup):
        super().execute(backup)
        for snapshot in backup.snapshots.all():
            # If snapshot creation was not started - delete it from waldur DB.
            if snapshot.state == CoreStates.CREATION_SCHEDULED:
                snapshot.decrease_backend_quotas_usage()
                snapshot.delete()
            else:
                snapshot.set_erred()
                snapshot.save(update_fields=["state"])


class DeleteIncompleteInstanceTask(core_tasks.Task):
    def execute(self, instance):
        with transaction.atomic():
            scopes = (
                [instance] + list(instance.volumes.all()) + list(instance.floating_ips)
            )
            for scope in scopes:
                if not scope.backend_id:
                    scope.decrease_backend_quotas_usage()


class ForceDeleteBackupTask(core_tasks.DeletionTask):
    def execute(self, backup):
        backup.snapshots.all().delete()
        super().execute(backup)


class VolumeExtendErredTask(core_tasks.ErrorStateTransitionTask):
    """Mark volume and its instance as erred on fail"""

    def execute(self, volume):
        super().execute(volume)
        if volume.instance is not None:
            super().execute(volume.instance)


class BaseDeleteExpiredResourcesTask(core_tasks.BackgroundTask):
    model = NotImplemented

    def _get_executor(self):
        raise NotImplementedError()

    @transaction.atomic
    def run(self):
        executor = self._get_delete_executor()
        resources = self.model.objects.filter(
            kept_until__lt=timezone.now(), state=CoreStates.OK
        )
        for resource in resources:
            executor.execute(resource)


class DeleteExpiredBackups(BaseDeleteExpiredResourcesTask):
    """Delete expired OpenStack backup resources that have reached their retention period."""

    name = "openstack.DeleteExpiredBackups"
    model = models.Backup

    def _get_delete_executor(self):
        from . import executors

        return executors.BackupDeleteExecutor


class DeleteExpiredSnapshots(BaseDeleteExpiredResourcesTask):
    """Delete expired OpenStack snapshot resources that have reached their retention period."""

    name = "openstack.DeleteExpiredSnapshots"
    model = models.Snapshot

    def _get_delete_executor(self):
        from . import executors

        return executors.SnapshotDeleteExecutor


class LimitedPerTypeThrottleMixin:
    def get_limit(self, resource):
        plugin_settings = getattr(settings, "WALDUR_OPENSTACK", {})
        limit_per_type = plugin_settings.get("MAX_CONCURRENT_PROVISION", {})
        model_name = get_resource_type(resource)
        default_limit = limit_per_type.get(model_name)
        tenant: models.Tenant = resource.tenant
        if tenant:
            key = f"max_concurrent_provision_{resource._meta.object_name.lower()}"
            settings_limit = tenant.service_settings.options.get(key)
            if settings_limit:
                return settings_limit
        return default_limit


class ThrottleProvisionTask(
    LimitedPerTypeThrottleMixin, structure_tasks.ThrottleProvisionTask
):
    pass


class ThrottleProvisionStateTask(
    LimitedPerTypeThrottleMixin, structure_tasks.ThrottleProvisionStateTask
):
    pass


class TenantResourcesPullTask(structure_tasks.BackgroundPullTask):
    def pull(self, tenant: models.Tenant):
        backend = OpenStackBackend(tenant.service_settings)
        backend.pull_tenant_volumes(tenant)
        backend.pull_tenant_snapshots(tenant)
        backend.pull_tenant_instances(tenant)


class TenantResourcesListPullTask(structure_tasks.BackgroundListPullTask):
    """Pull OpenStack tenant resources like instances, volumes, and snapshots from backend."""

    name = "openstack.tenant_resources_list_pull_task"
    pull_task = TenantResourcesPullTask
    model = models.Tenant


class TenantSubresourcesPullTask(structure_tasks.BackgroundPullTask):
    def pull(self, tenant: models.Tenant):
        backend = OpenStackBackend(tenant.service_settings)
        backend.pull_tenant_security_groups(tenant)
        backend.pull_tenant_server_groups(tenant)
        backend.pull_tenant_floating_ips(tenant)
        backend.pull_tenant_networks(tenant)
        backend.pull_tenant_subnets(tenant)
        backend.pull_tenant_ports(tenant)
        backend.pull_tenant_routers(tenant)
        backend.pull_tenant_load_balancers(tenant)
        backend.pull_tenant_pools(tenant)
        backend.pull_tenant_pool_members(tenant)
        backend.pull_tenant_healthmonitors(tenant)
        backend.pull_tenant_listeners(tenant)
        backend.pull_tenant_network_rbac_policies(tenant)


class TenantSubresourcesListPullTask(structure_tasks.BackgroundListPullTask):
    """Pull OpenStack tenant subresources like security groups, networks, subnets, and ports from backend."""

    name = "openstack.tenant_subresources_list_pull_task"
    pull_task = TenantSubresourcesPullTask
    model = models.Tenant


class TenantPropertiesPullTask(structure_tasks.BackgroundPullTask):
    def pull(self, tenant: models.Tenant):
        backend = OpenStackBackend(tenant.service_settings)
        backend.pull_tenant_volume_types(tenant)
        backend.pull_tenant_volume_availability_zones(tenant)
        backend.pull_tenant_instance_availability_zones(tenant)
        backend.pull_tenant_flavors(tenant)
        backend.pull_tenant_images(tenant)


class TenantPropertiesListPullTask(structure_tasks.BackgroundListPullTask):
    """Pull OpenStack tenant properties like flavors, images, and volume types from backend."""

    name = "openstack.tenant_properties_list_pull_task"
    pull_task = TenantPropertiesPullTask
    model = models.Tenant


@shared_task
def create_offerings_task(serialized_tenant):
    tenant = core_utils.deserialize_instance(serialized_tenant)
    create_offerings_for_volume_and_instance(tenant)


@shared_task(name="openstack.mark_stuck_updating_tenants_as_erred")
def mark_stuck_updating_tenants_as_erred():
    tenants_stuck = models.Tenant.objects.exclude(update_triggered=None).filter(
        update_triggered__lte=timezone.now() - timezone.timedelta(hours=2),
        state=CoreStates.UPDATING,
    )
    for tenant in tenants_stuck:
        tenant.set_erred()
        tenant.save(update_fields=["state"])


@shared_task(name="openstack.PullTenantUsageQuotas")
def pull_tenant_usage_quotas(tenant_id):
    """Pull quotas for a single tenant during usage billing poll."""
    try:
        tenant = models.Tenant.objects.get(pk=tenant_id)
    except models.Tenant.DoesNotExist:
        logger.warning("Tenant %s not found for usage quota pull", tenant_id)
        return
    try:
        backend = OpenStackBackend(tenant.service_settings)
        backend.pull_tenant_quotas(tenant)
    except Exception:
        logger.exception(
            "Failed to pull quotas for tenant %s during usage billing poll",
            tenant.uuid,
        )


@shared_task(name="openstack.TenantUsageBillingPoll")
def tenant_usage_billing_poll():
    """Poll quota usage for tenants whose offering uses USAGE billing.

    Runs on a 30-min base tick. Per-offering throttle (via Django cache)
    ensures each offering is polled at its configured interval.
    Dispatches a separate Celery task per tenant to leverage multiple workers.
    """
    from django.core.cache import cache

    from waldur_mastermind.marketplace import models as marketplace_models
    from waldur_mastermind.marketplace.enums import (
        OPENSTACK_TENANT_OFFERING,
        BillingTypes,
    )

    offerings = marketplace_models.Offering.objects.filter(
        type=OPENSTACK_TENANT_OFFERING,
        components__billing_type=BillingTypes.USAGE,
    ).distinct()

    for offering in offerings:
        interval_minutes = offering.plugin_options.get(
            "usage_poll_interval_minutes", 60
        )
        interval_minutes = max(15, min(1440, interval_minutes))

        throttle_key = f"usage_poll_throttle_{offering.uuid}"
        if not cache.add(throttle_key, True, timeout=interval_minutes * 60):
            continue

        resources = marketplace_models.Resource.objects.filter(
            offering=offering,
            state=marketplace_models.Resource.States.OK,
        ).prefetch_related("scope")

        for resource in resources:
            tenant = resource.scope
            if not tenant or not isinstance(tenant, models.Tenant):
                continue
            if tenant.state != CoreStates.OK:
                continue
            pull_tenant_usage_quotas.delay(tenant.pk)
