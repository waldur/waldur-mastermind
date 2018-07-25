from waldur_core.core import tasks as core_tasks

from . import models


class TenantCreateErrorTask(core_tasks.ErrorStateTransitionTask):

    def execute(self, tenant):
        super(TenantCreateErrorTask, self).execute(tenant)
        # Delete network and subnet if they were not created on backend,
        # mark as erred if they were created
        network = tenant.networks.first()
        subnet = network.subnets.first()
        if subnet.state == models.SubNet.States.CREATION_SCHEDULED:
            subnet.delete()
        else:
            super(TenantCreateErrorTask, self).execute(subnet)
        if network.state == models.Network.States.CREATION_SCHEDULED:
            network.delete()
        else:
            super(TenantCreateErrorTask, self).execute(network)


class TenantCreateSuccessTask(core_tasks.StateTransitionTask):

    def execute(self, tenant):
        network = tenant.networks.first()
        subnet = network.subnets.first()
        self.state_transition(network, 'set_ok')
        self.state_transition(subnet, 'set_ok')
        self.state_transition(tenant, 'set_ok')
        return super(TenantCreateSuccessTask, self).execute(tenant)


class TenantPullQuotas(core_tasks.BackgroundTask):
    name = 'openstack.TenantPullQuotas'

    def is_equal(self, other_task):
        return self.name == other_task.get('name')

    def run(self):
        from . import executors
        for tenant in models.Tenant.objects.filter(state=models.Tenant.States.OK):
            executors.TenantPullQuotasExecutor.execute(tenant)
