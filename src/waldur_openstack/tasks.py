import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from waldur_core.core import models as core_models
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.structure import tasks as structure_tasks
from waldur_core.structure.registry import get_resource_type
from waldur_openstack.backend import OpenStackBackend

from . import models, signals

logger = logging.getLogger(__name__)


class TenantCreateErrorTask(core_tasks.ErrorStateTransitionTask):
    def execute(self, tenant):
        super().execute(tenant)
        # Delete network and subnet if they were not created on backend,
        # mark as erred if they were created
        network = tenant.networks.first()
        subnet = network.subnets.first()
        if subnet.state == models.SubNet.States.CREATION_SCHEDULED:
            subnet.delete()
        else:
            super().execute(subnet)
        if network.state == models.Network.States.CREATION_SCHEDULED:
            network.delete()
        else:
            super().execute(network)


class TenantCreateSuccessTask(core_tasks.StateTransitionTask):
    def execute(self, tenant):
        from . import executors

        executors.TenantPullExecutor.execute(tenant)
        return super().execute(tenant)


class TenantPullQuotas(core_tasks.BackgroundTask):
    name = "openstack.TenantPullQuotas"

    def is_equal(self, other_task):
        return self.name == other_task.get("name")

    def run(self):
        from . import executors

        for tenant in models.Tenant.objects.filter(state=models.Tenant.States.OK):
            executors.TenantPullQuotasExecutor.execute(tenant)


class SendSignalTenantPullSucceeded(core_tasks.Task):
    @classmethod
    def get_description(cls, *args, **kwargs):
        return "Send tenant_pull_succeeded signal."

    def execute(self, tenant):
        signals.tenant_pull_succeeded.send(models.Tenant, instance=tenant)


@shared_task(name="openstack.mark_as_erred_old_tenants_in_deleting_state")
def mark_as_erred_old_tenants_in_deleting_state():
    models.Tenant.objects.filter(
        modified__lte=timezone.now() - timezone.timedelta(days=1),
        state=models.Tenant.States.DELETING,
    ).update(
        state=models.Tenant.States.ERRED,
        error_message="Deletion error. Deleting took more than a day.",
    )


@shared_task
def check_existence_of_tenant(serialized_tenant):
    tenant = core_utils.deserialize_instance(serialized_tenant)
    backend = tenant.get_backend()

    if backend.does_tenant_exist_in_backend(tenant) is False:
        raise Exception(f"Tenant {tenant} does not exist in backend.")


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
        instance.floating_ips.update(state=models.FloatingIP.States.OK)


class SetInstanceErredTask(core_tasks.ErrorStateTransitionTask):
    """Mark instance as erred and delete resources that were not created."""

    def execute(self, instance):
        super().execute(instance)

        # delete volumes if they were not created on backend,
        # mark as erred if creation was started, but not ended,
        # leave as is, if they are OK.
        for volume in instance.volumes.all():
            if volume.state == models.Volume.States.CREATION_SCHEDULED:
                volume.delete()
            elif volume.state == models.Volume.States.OK:
                pass
            else:
                volume.set_erred()
                volume.save(update_fields=["state"])

        # set instance floating IPs as free, delete not created ones.
        instance.floating_ips.filter(backend_id="").delete()
        instance.floating_ips.update(state=models.FloatingIP.States.OK)


class SetBackupErredTask(core_tasks.ErrorStateTransitionTask):
    """Mark DR backup and all related resources that are not in state OK as Erred"""

    def execute(self, backup):
        super().execute(backup)
        for snapshot in backup.snapshots.all():
            # If snapshot creation was not started - delete it from waldur DB.
            if snapshot.state == models.Snapshot.States.CREATION_SCHEDULED:
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

    def is_equal(self, other_task):
        return self.name == other_task.get("name")

    def _get_executor(self):
        raise NotImplementedError()

    @transaction.atomic
    def run(self):
        executor = self._get_delete_executor()
        resources = self.model.objects.filter(
            kept_until__lt=timezone.now(), state=core_models.StateMixin.States.OK
        )
        for resource in resources:
            executor.execute(resource)


class DeleteExpiredBackups(BaseDeleteExpiredResourcesTask):
    name = "openstack.DeleteExpiredBackups"
    model = models.Backup

    def _get_delete_executor(self):
        from . import executors

        return executors.BackupDeleteExecutor


class DeleteExpiredSnapshots(BaseDeleteExpiredResourcesTask):
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
        backend.pull_tenant_instances(tenant)
        backend.pull_tenant_volumes(tenant)
        backend.pull_tenant_snapshots(tenant)


class TenantResourcesListPullTask(structure_tasks.BackgroundListPullTask):
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
        backend.pull_tenant_routers(tenant)
        backend.pull_tenant_ports(tenant)


class TenantSubresourcesListPullTask(structure_tasks.BackgroundListPullTask):
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
    name = "openstack.tenant_properties_list_pull_task"
    pull_task = TenantPropertiesPullTask
    model = models.Tenant
