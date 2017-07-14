from __future__ import unicode_literals

from django.apps import AppConfig
from django.db.models import signals


class ExpertsConfig(AppConfig):
    name = 'nodeconductor_assembly_waldur.experts'
    verbose_name = 'Experts'

    def ready(self):
        ExpertRequest = self.get_model('ExpertRequest')
        ExpertBid = self.get_model('ExpertBid')

        from . import handlers

        signals.post_save.connect(
            handlers.log_expert_request_creation,
            sender=ExpertRequest,
            dispatch_uid='nodeconductor_assembly_waldur.experts.log_expert_request_creation',
        )

        signals.post_save.connect(
            handlers.log_expert_request_state_changed,
            sender=ExpertRequest,
            dispatch_uid='nodeconductor_assembly_waldur.experts.log_expert_request_state_changed',
        )

        signals.post_save.connect(
            handlers.log_expert_bid_creation,
            sender=ExpertBid,
            dispatch_uid='nodeconductor_assembly_waldur.experts.log_expert_bid_creation',
        )
