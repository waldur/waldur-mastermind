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


#: Fallback when the operator blanked the setting out of the database. The
#: same value is the Constance default.
DEFAULT_ISSUE_KEY_PREFIX = "WLD"


def build_backend_id(uuid, marker: str = "") -> str:
    """Compose the id of a locally-created ticket, comment or attachment.

    Shape is ``<PREFIX>[-<marker>]-<8 hex chars>``, e.g. ``WLD-A1B2C3D4`` for a
    ticket and ``WLD-C-A1B2C3D4`` for its comment. The prefix is operator
    configurable; ids already stored on an object are never recomputed, so a
    changed prefix only affects objects created after the change.
    """
    # Normalised rather than trusted: the setting is validated on write, but a
    # value stored before that validation existed, or written straight into the
    # database, would otherwise end up inside every ticket key. A stray newline
    # there reaches the mail subject and makes every support notification raise.
    prefix = (
        config.WALDUR_SUPPORT_ISSUE_KEY_PREFIX or ""
    ).strip().upper() or DEFAULT_ISSUE_KEY_PREFIX
    parts = [prefix, marker, uuid.hex[:8].upper()]
    return "-".join(part for part in parts if part)


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

    def get_available_statuses(self, issue) -> list[str]:
        """Statuses Waldur may move this issue to.

        Empty whenever the remote service desk owns the ticket lifecycle: for
        Jira, Zammad and SMAX the status only ever travels inbound, through
        `sync_single_issue` and the webhook receivers. Only a backend that
        answers `update_is_available` with True has any business offering
        transitions here.
        """
        return []

    def issue_is_active(self, issue) -> bool:
        """Is the ticket still open for changes?

        `resolved` comes from `IssueStatus.check_success_status`, which answers
        None both while a ticket is being worked on and whenever the status
        registry cannot classify it: a missing terminal type, an unknown status
        name, an unexpected type value. Every one of those reads as active, so
        this predicate fails open on a misconfigured registry rather than
        locking a deployment out of its own tickets.

        Each call costs several queries, since `resolved` is an uncached
        property. `BasicBackend` overrides this with the stored resolution date,
        which it keeps in step itself; a backend that cannot do the same should
        keep using this.
        """
        return issue is not None and issue.resolved is None

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
