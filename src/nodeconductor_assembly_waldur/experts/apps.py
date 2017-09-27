from __future__ import unicode_literals

from django.apps import AppConfig
from django.db.models import signals


class ExpertsConfig(AppConfig):
    name = 'nodeconductor_assembly_waldur.experts'
    verbose_name = 'Experts'

    def ready(self):
        from nodeconductor_assembly_waldur.invoices import registrators as invoices_registrators
        from . import handlers, registrators

        ExpertRequest = self.get_model('ExpertRequest')
        ExpertBid = self.get_model('ExpertBid')

        invoices_registrators.RegistrationManager.add_registrator(
            ExpertRequest,
            registrators.ExpertRequestRegistrator
        )

        signals.post_save.connect(
            handlers.add_completed_expert_request_to_invoice,
            sender=ExpertRequest,
            dispatch_uid='nodeconductor_assembly_waldur.experts.handlers.add_completed_expert_request_to_invoice',
        )

        signals.pre_delete.connect(
            handlers.terminate_invoice_when_expert_request_deleted,
            sender=ExpertRequest,
            dispatch_uid='nodeconductor_assembly_waldur.experts.handlers.terminate_invoice_when_expert_request_deleted',
        )

        signals.post_save.connect(
            handlers.log_expert_request_creation,
            sender=ExpertRequest,
            dispatch_uid='nodeconductor_assembly_waldur.experts.handlers.log_expert_request_creation',
        )

        signals.post_save.connect(
            handlers.log_expert_request_state_changed,
            sender=ExpertRequest,
            dispatch_uid='nodeconductor_assembly_waldur.experts.handlers.log_expert_request_state_changed',
        )

        signals.post_save.connect(
            handlers.log_expert_bid_creation,
            sender=ExpertBid,
            dispatch_uid='nodeconductor_assembly_waldur.experts.handlers.log_expert_bid_creation',
        )
