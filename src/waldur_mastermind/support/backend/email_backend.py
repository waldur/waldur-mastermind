import logging

from waldur_core.core.utils import broadcast_mail

from . import SupportBackend

logger = logging.getLogger(__name__)


class EmailSupportBackend(SupportBackend):
    """Support backend that communicates with providers via email."""

    backend_name = "email"

    def __init__(self, settings_dict=None, provider_helpdesk=None):
        self.settings_dict = settings_dict or {}
        self.provider_helpdesk = provider_helpdesk

    @classmethod
    def from_settings(cls, settings_dict, provider_helpdesk=None):
        return cls(settings_dict=settings_dict, provider_helpdesk=provider_helpdesk)

    def create_issue(self, issue):
        issue.backend_id = f"WLD-E-{issue.uuid.hex[:8].upper()}"
        issue.key = issue.backend_id
        issue.save()

        if self.provider_helpdesk:
            recipients = self.provider_helpdesk.get_notification_emails()
            if recipients:
                context = {
                    "issue": issue,
                    "provider_helpdesk": self.provider_helpdesk,
                }
                try:
                    broadcast_mail(
                        "support",
                        "provider_email_new_ticket",
                        context,
                        recipients,
                    )
                except Exception:
                    logger.exception(
                        "Failed to send new ticket email for issue %s", issue.key
                    )

    def update_issue(self, issue):
        issue.save()

    def delete_issue(self, issue):
        return

    def create_comment(self, comment):
        comment.backend_id = f"WLD-EC-{comment.uuid.hex[:8].upper()}"
        comment.save(update_fields=["backend_id"])

        if self.provider_helpdesk:
            recipients = self.provider_helpdesk.get_notification_emails()
            if recipients:
                context = {
                    "comment": comment,
                    "issue": comment.issue,
                    "provider_helpdesk": self.provider_helpdesk,
                }
                try:
                    broadcast_mail(
                        "support",
                        "provider_email_comment",
                        context,
                        recipients,
                    )
                except Exception:
                    logger.exception(
                        "Failed to send comment email for issue %s",
                        comment.issue.key,
                    )

    def update_comment(self, comment):
        return

    def delete_comment(self, comment):
        return

    def create_attachment(self, attachment):
        attachment.backend_id = f"WLD-EA-{attachment.uuid.hex[:8].upper()}"
        attachment.save(update_fields=["backend_id"])

    def delete_attachment(self, attachment):
        return

    def get_users(self):
        return

    def pull_priorities(self):
        return

    def create_issue_links(self, *args, **kwargs):
        return

    def get_issue_details(self):
        return {}

    def update_is_available(self, issue=None):
        return True

    def destroy_is_available(self, issue=None):
        return True

    def attachment_destroy_is_available(self, attachment=None):
        return True
