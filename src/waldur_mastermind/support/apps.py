from django.apps import AppConfig
from django.db.models import signals


class SupportConfig(AppConfig):
    name = "waldur_mastermind.support"
    verbose_name = "HelpDesk"

    def ready(self):
        from . import handlers

        Issue = self.get_model("Issue")
        Attachment = self.get_model("Attachment")
        Comment = self.get_model("Comment")

        signals.post_save.connect(
            handlers.log_issue_save,
            sender=Issue,
            dispatch_uid="waldur_mastermind.support.handlers.log_issue_save",
        )

        signals.post_delete.connect(
            handlers.log_issue_delete,
            sender=Issue,
            dispatch_uid="waldur_mastermind.support.handlers.log_issue_delete",
        )

        signals.post_save.connect(
            handlers.log_attachment_save,
            sender=Attachment,
            dispatch_uid="waldur_mastermind.support.handlers.log_attachment_save",
        )

        signals.post_delete.connect(
            handlers.log_attachment_delete,
            sender=Attachment,
            dispatch_uid="waldur_mastermind.support.handlers.log_attachment_delete",
        )

        signals.post_save.connect(
            handlers.send_comment_added_notification,
            sender=Comment,
            dispatch_uid="waldur_mastermind.support.handlers.send_comment_added_notification",
        )

        signals.post_save.connect(
            handlers.send_issue_updated_notification,
            sender=Issue,
            dispatch_uid="waldur_mastermind.support.handlers.send_issue_updated_notification",
        )

        signals.post_save.connect(
            handlers.create_feedback_if_issue_has_been_resolved,
            sender=Issue,
            dispatch_uid="waldur_mastermind.support.handlers.create_feedback_if_issue_has_been_resolved",
        )

        signals.post_save.connect(
            handlers.dispatch_routing_on_issue_create,
            sender=Issue,
            dispatch_uid="waldur_mastermind.support.handlers.dispatch_routing_on_issue_create",
        )

        signals.post_save.connect(
            handlers.forward_comment_to_children,
            sender=Comment,
            dispatch_uid="waldur_mastermind.support.handlers.forward_comment_to_children",
        )

        signals.post_save.connect(
            handlers.propagate_comment_to_parent,
            sender=Comment,
            dispatch_uid="waldur_mastermind.support.handlers.propagate_comment_to_parent",
        )
