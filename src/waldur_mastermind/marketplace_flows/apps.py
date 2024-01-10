from django.apps import AppConfig
from django.db.models import signals


class MarketplaceFlowsConfig(AppConfig):
    name = "waldur_mastermind.marketplace_flows"

    def ready(self):
        from waldur_mastermind.support import models as support_models

        from . import handlers, models

        signals.post_save.connect(
            handlers.process_flow_state_change,
            sender=models.FlowTracker,
            dispatch_uid="waldur_mastermind.marketplace_flows.process_flow_state_change",
        )

        signals.post_save.connect(
            handlers.approve_reject_offering_state_request_when_related_issue_is_resolved,
            sender=support_models.Issue,
            dispatch_uid="waldur_mastermind.marketplace_flows.approve_reject_offering_state_request_when_related_issue_is_resolved",
        )
