import importlib
import logging

from constance import config

from waldur_mastermind.support import models

logger = logging.getLogger(__name__)


class SupportBackendType:
    BASIC = "basic"
    ATLASSIAN = "atlassian"
    ZAMMAD = "zammad"
    SMAX = "smax"


class SupportedFormat:
    HTML = "html"
    TEXT = "text"


def get_active_backend() -> "SupportBackend":
    backend_type = config.WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE
    if backend_type == SupportBackendType.ATLASSIAN:
        path = "waldur_mastermind.support.backend.atlassian:ServiceDeskBackend"
    elif backend_type == SupportBackendType.ZAMMAD:
        path = "waldur_mastermind.support.backend.zammad:ZammadServiceBackend"
    elif backend_type == SupportBackendType.SMAX:
        path = "waldur_mastermind.support.backend.smax:SmaxServiceBackend"
    elif backend_type == SupportBackendType.BASIC:
        path = "waldur_mastermind.support.backend.basic:BasicBackend"
    else:
        path = "waldur_mastermind.support.backend.basic:BasicBackend"

    module_path, class_name = path.split(":")
    module = importlib.import_module(module_path)
    klass = getattr(module, class_name)
    return klass()


class SupportBackendError(Exception):
    pass


class SupportBackend:
    """Interface for support backend"""

    backend_name = None
    summary_max_length = 255
    message_format = SupportedFormat.TEXT

    def create_issue(self, issue):
        return

    def update_issue(self, issue):
        return

    def delete_issue(self, issue):
        return

    def create_comment(self, comment):
        return

    def update_comment(self, comment):
        return

    def delete_comment(self, comment):
        return

    def create_attachment(self, attachment):
        return

    def delete_attachment(self, attachment):
        return

    def get_users(self):
        """
        This method should return all users that are related to support project on backend.

        Each user should be represented as not saved SupportUser instance.
        """
        return

    def pull_priorities(self):
        """
        This method should pull priorities from backend and to the local database.
        """
        return

    def update_is_available(self, issue=None):
        return False

    def destroy_is_available(self, issue=None):
        return False

    def comment_create_is_available(self, issue=None):
        return True

    def comment_update_is_available(self, comment=None):
        return True

    def comment_destroy_is_available(self, comment=None):
        return True

    def attachment_destroy_is_available(self, attachment=None):
        return False

    def attachment_create_is_available(self, issue=None):
        return True

    def pull_support_users(self):
        return

    def get_confirmation_comment_template(self, issue_type):
        try:
            tmpl = models.TemplateConfirmationComment.objects.get(issue_type=issue_type)
        except models.TemplateConfirmationComment.DoesNotExist:
            try:
                tmpl = models.TemplateConfirmationComment.objects.get(
                    issue_type="default"
                )
            except models.TemplateConfirmationComment.DoesNotExist:
                logger.debug(
                    "A confirmation comment hasn't been created, because a template does not exist."
                )
                return
        return tmpl.template

    def sync_single_issue(self, issue):
        """
        Synchronize a single issue's data from backend.
        Used by both webhooks and manual sync for consistency.
        """
        return

    def sync_issues(self, *args, **kwargs):
        return

    def get_issue_details(self, *args, **kwargs):
        return {}

    def create_issue_links(self, issue, linked_issues):
        return

    def create_confirmation_comment(self, issue, comment_tmpl=""):
        return


def get_backend_for_provider(provider_helpdesk) -> SupportBackend:
    """Factory to create a backend instance for a given ProviderHelpdesk.

    For each backend type, creates a provider-scoped backend using the
    provider's settings dict. If settings are empty/incomplete, backends
    fall back to global Constance settings — so a provider with no custom
    settings effectively uses the operator's global backend config.
    """
    backend_type = provider_helpdesk.backend_type
    settings_dict = provider_helpdesk.settings or {}

    if backend_type == "basic":
        from .basic import BasicBackend

        return BasicBackend.from_settings(settings_dict)
    elif backend_type == "email":
        from .email_backend import EmailSupportBackend

        return EmailSupportBackend.from_settings(settings_dict, provider_helpdesk)
    elif backend_type == SupportBackendType.ATLASSIAN:
        from .atlassian import ServiceDeskBackend

        return ServiceDeskBackend.from_settings(settings_dict)
    elif backend_type == SupportBackendType.ZAMMAD:
        from .zammad import ZammadServiceBackend

        return ZammadServiceBackend.from_settings(settings_dict)
    elif backend_type == SupportBackendType.SMAX:
        from .smax import SmaxServiceBackend

        return SmaxServiceBackend.from_settings(settings_dict)
    else:
        raise SupportBackendError(f"Unknown provider backend type: {backend_type}")
