from django.apps import AppConfig
from django.db.models import signals


class SupportConfig(AppConfig):
    name = 'nodeconductor_assembly_waldur.support'
    verbose_name = 'HelpDesk'

    def ready(self):
        from . import handlers

        Issue = self.get_model('Issue')
        Offering = self.get_model('Offering')
        Comment = self.get_model('Comment')

        signals.post_save.connect(
            handlers.log_issue_save,
            sender=Issue,
            dispatch_uid='nodeconductor_assembly_waldur.support.handlers.log_issue_save',
        )

        signals.post_delete.connect(
            handlers.log_issue_delete,
            sender=Issue,
            dispatch_uid='nodeconductor_assembly_waldur.support.handlers.log_issue_delete',
        )

        signals.post_save.connect(
            handlers.log_offering_state_changed,
            sender=Offering,
            dispatch_uid='nodeconductor_assembly_waldur.support.handlers.log_offering_state_changed',
        )

        signals.post_save.connect(
            handlers.send_comment_added_notification,
            sender=Comment,
            dispatch_uid='nodeconductor_assembly_waldur.support.handlers.send_comment_added_notification'
        )

        signals.post_save.connect(
            handlers.send_issue_updated_notification,
            sender=Issue,
            dispatch_uid='nodeconductor_assembly_waldur.support.handlers.send_issue_updated_notification'
        )

