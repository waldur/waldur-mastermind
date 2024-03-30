from celery import shared_task
from django.utils import timezone

from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils

from . import models, signals


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
        network = tenant.networks.first()
        subnet = network.subnets.first()
        self.state_transition(network, "set_ok")
        self.state_transition(subnet, "set_ok")
        self.state_transition(tenant, "set_ok")

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
