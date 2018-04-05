from __future__ import unicode_literals

from django.apps import AppConfig
from django.db.models import signals


class ExpertsConfig(AppConfig):
    name = 'waldur_mastermind.experts'
    verbose_name = 'Experts'

    def ready(self):
        from waldur_core.structure import models as structure_models
        from waldur_mastermind.invoices import registrators as invoices_registrators
        from waldur_mastermind.support.models import Comment

        from . import handlers, registrators, quotas

        ExpertRequest = self.get_model('ExpertRequest')
        ExpertBid = self.get_model('ExpertBid')
        ExpertContract = self.get_model('ExpertContract')

        invoices_registrators.RegistrationManager.add_registrator(
            ExpertRequest,
            registrators.ExpertRequestRegistrator
        )

        quotas.add_quota_field()

        signals.post_save.connect(
            handlers.update_project_quota_when_request_is_saved,
            sender=ExpertRequest,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'update_project_quota_when_request_is_saved',
        )

        signals.pre_delete.connect(
            handlers.update_project_quota_when_request_is_deleted,
            sender=ExpertRequest,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'update_project_quota_when_request_is_deleted',
        )

        signals.post_save.connect(
            handlers.update_customer_quota_when_request_is_saved,
            sender=ExpertRequest,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'update_customer_quota_when_request_is_saved',
        )

        signals.pre_delete.connect(
            handlers.update_customer_quota_when_request_is_deleted,
            sender=ExpertRequest,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'update_customer_quota_when_request_is_deleted',
        )

        signals.post_save.connect(
            handlers.add_completed_expert_request_to_invoice,
            sender=ExpertRequest,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'add_completed_expert_request_to_invoice',
        )

        signals.pre_delete.connect(
            handlers.terminate_invoice_when_expert_request_deleted,
            sender=ExpertRequest,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'terminate_invoice_when_expert_request_deleted',
        )

        signals.post_save.connect(
            handlers.log_expert_request_creation,
            sender=ExpertRequest,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'log_expert_request_creation',
        )

        signals.post_save.connect(
            handlers.log_expert_request_state_changed,
            sender=ExpertRequest,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'log_expert_request_state_changed',
        )

        signals.post_save.connect(
            handlers.log_expert_bid_creation,
            sender=ExpertBid,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'log_expert_bid_creation',
        )

        signals.post_save.connect(
            handlers.notify_expert_providers_about_new_request,
            sender=ExpertRequest,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'notify_expert_providers_about_new_request',
        )

        signals.post_save.connect(
            handlers.notify_customer_owners_about_new_bid,
            sender=ExpertBid,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'notify_customer_owners_about_new_bid',
        )

        signals.post_save.connect(
            handlers.notify_customer_owners_about_new_contract,
            sender=ExpertContract,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'notify_customer_owners_about_new_contract',
        )

        signals.post_save.connect(
            handlers.update_customer_quota_when_contract_is_created,
            sender=ExpertContract,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'update_customer_quota_when_contract_is_created',
        )

        signals.pre_delete.connect(
            handlers.update_customer_quota_when_contract_is_deleted,
            sender=ExpertContract,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'update_customer_quota_when_contract_is_deleted',
        )

        signals.post_save.connect(
            handlers.set_project_name_on_expert_request_creation,
            sender=ExpertRequest,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'set_project_name_on_expert_request_creation',
        )

        signals.post_save.connect(
            handlers.update_expert_request_on_project_name_update,
            sender=structure_models.Project,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'update_expert_request_on_project_name_update',
        )

        signals.post_save.connect(
            handlers.set_team_name_on_expert_contract_creation,
            sender=ExpertContract,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'set_team_name_on_expert_contract_creation',
        )

        signals.post_save.connect(
            handlers.update_expert_contract_on_project_name_update,
            sender=structure_models.Project,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'update_expert_contract_on_project_name_update',
        )

        signals.post_save.connect(
            handlers.send_expert_comment_added_notification,
            sender=Comment,
            dispatch_uid='waldur_mastermind.experts.handlers.'
                         'send_expert_comment_added_notification',
        )
