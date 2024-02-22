import importlib
import logging

from constance import config

from waldur_mastermind.support import models

logger = logging.getLogger(__name__)


class SupportBackendType:
    ATLASSIAN = "atlassian"
    ZAMMAD = "zammad"
    SMAX = "smax"


class SupportedFormat:
    HTML = "html"
    TEXT = "text"


def get_active_backend():
    backend_type = config.WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE
    if backend_type == SupportBackendType.ATLASSIAN:
        path = "waldur_mastermind.support.backend.atlassian:ServiceDeskBackend"
    elif backend_type == SupportBackendType.ZAMMAD:
        path = "waldur_mastermind.support.backend.zammad:ZammadServiceBackend"
    elif backend_type == SupportBackendType.SMAX:
        path = "waldur_mastermind.support.backend.smax:SmaxServiceBackend"
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
        raise NotImplementedError

    def update_issue(self, issue):
        raise NotImplementedError

    def delete_issue(self, issue):
        raise NotImplementedError

    def create_comment(self, comment):
        raise NotImplementedError

    def update_comment(self, comment):
        raise NotImplementedError

    def delete_comment(self, comment):
        raise NotImplementedError

    def create_attachment(self, attachment):
        raise NotImplementedError

    def delete_attachment(self, attachment):
        raise NotImplementedError

    def get_users(self):
        """
        This method should return all users that are related to support project on backend.

        Each user should be represented as not saved SupportUser instance.
        """
        raise NotImplementedError

    def pull_priorities(self):
        """
        This method should pull priorities from backend and to the local database.
        """
        raise NotImplementedError

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
        raise NotImplementedError

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

    def sync_issues(self, *args, **kwargs):
        return

    def get_issue_details(self, *args, **kwargs):
        return {}
