from django.apps import AppConfig
from django.db.models import signals


class MarketplaceFlowsConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_flows'

    def ready(self):
        from . import handlers, models

        signals.post_save.connect(
            handlers.process_flow_state_change,
            sender=models.FlowTracker,
            dispatch_uid='waldur_mastermind.marketplace_flows.process_flow_state_change',
        )
