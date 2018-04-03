from django.apps import AppConfig
from django.db.models import signals


class SupportConfig(AppConfig):
    name = 'waldur_mastermind.support'
    verbose_name = 'HelpDesk'

    def ready(self):
        from waldur_core.quotas import fields as quota_fields
        from waldur_core.structure import models as structure_models

        from . import handlers

        Issue = self.get_model('Issue')
        Offering = self.get_model('Offering')
        Comment = self.get_model('Comment')

        structure_models.Project.add_quota_field(
            name='nc_offering_count',
            quota_field=quota_fields.CounterQuotaField(
                target_models=[Offering],
                path_to_scope='project',
            )
        )
        structure_models.Customer.add_quota_field(
            name='nc_offering_count',
            quota_field=quota_fields.CounterQuotaField(
                target_models=[Offering],
                path_to_scope='project.customer',
            )
        )

        signals.post_save.connect(
            handlers.log_issue_save,
            sender=Issue,
            dispatch_uid='waldur_mastermind.support.handlers.log_issue_save',
        )

        signals.post_delete.connect(
            handlers.log_issue_delete,
            sender=Issue,
            dispatch_uid='waldur_mastermind.support.handlers.log_issue_delete',
        )

        signals.post_save.connect(
            handlers.log_offering_created,
            sender=Offering,
            dispatch_uid='waldur_mastermind.support.handlers.log_offering_created',
        )

        signals.pre_delete.connect(
            handlers.log_offering_deleted,
            sender=Offering,
            dispatch_uid='waldur_mastermind.support.handlers.log_offering_deleted',
        )

        signals.post_save.connect(
            handlers.log_offering_state_changed,
            sender=Offering,
            dispatch_uid='waldur_mastermind.support.handlers.log_offering_state_changed',
        )

        signals.post_save.connect(
            handlers.send_comment_added_notification,
            sender=Comment,
            dispatch_uid='waldur_mastermind.support.handlers.send_comment_added_notification'
        )

        signals.post_save.connect(
            handlers.send_issue_updated_notification,
            sender=Issue,
            dispatch_uid='waldur_mastermind.support.handlers.send_issue_updated_notification'
        )
