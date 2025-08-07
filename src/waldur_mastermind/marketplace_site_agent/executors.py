from waldur_mastermind.marketplace.executors import MarketplaceActionExecutor
from waldur_mastermind.marketplace_site_agent import tasks


class AgentResourcePullExecutor(MarketplaceActionExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.sync_resource.si(serialized_instance)
