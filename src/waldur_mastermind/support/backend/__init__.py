import importlib
import logging
from urllib.parse import urlsplit, urlunsplit

from django.db.models import Q

from waldur_core.configuration import config
from waldur_core.core.permissions import has_staff_access
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

    #: May the author of a comment change or remove it themselves? Off unless
    #: Waldur is the system of record: on a remote service desk the comment has
    #: already reached the agents, and a change made through the integration
    #: account would rewrite it under that account's name.
    comment_author_update_is_supported = False
    comment_author_destroy_is_supported = False

    #: The ``backend_name`` of the attachments this backend holds, where it is
    #: not ``backend_name``. ``get_backend_for_provider`` sets one per helpdesk.
    attachment_backend_name: str | None = None

    #: The settings that say which desk a remote backend's tickets live on:
    #: the same id names the same ticket only on the same desk. None for a
    #: backend whose tickets live in Waldur itself.
    desk_settings: tuple[str, ...] | None = None

    def get_attachment_backend_name(self) -> str:
        return self.attachment_backend_name or self.backend_name

    def holds_attachment(self, attachment) -> bool:
        """Is the attachment filed under a name this desk files under?

        A provider helpdesk's desk files new attachments under the helpdesk's
        name, but a file an earlier sync pulled in carries the plain one.
        """
        return attachment.backend_name in (
            self.backend_name,
            self.get_attachment_backend_name(),
        )

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


def issue_is_routed(issue) -> bool:
    """Is the ticket part of a provider routing, as a parent or as a child?"""
    return issue.parent_issue_id is not None or issue.child_issues.exists()


# support.permissions describes this rule in the API schema (_changes_comment);
# change the two together.
def comment_change_is_permitted(
    request, comment, author_may_change: bool, is_routed=issue_is_routed
) -> bool:
    """Staff may change any comment; its author only where the backend allows.

    Staff here means a request that may act as staff: a staff user's personal
    access token without the staff scope is held to the author's rule.

    A comment pulled in from a remote service desk has no local user behind its
    author, so it never matches and stays with staff.

    Nor may the author change a comment that has been copied to another ticket.
    Routing to a provider helpdesk copies public comments between the parent
    ticket and its children, and only on creation: the copy carries no link back,
    so an edit or a deletion would leave the provider holding the original text
    while the author is told it is gone.

    `is_routed` answers that for the comment's ticket. A caller checking many
    comments of one ticket passes a cached one, to avoid a query per comment.
    """
    if has_staff_access(request):
        return True
    user = request.user
    author_user_id = comment.author.user_id
    if not author_may_change or author_user_id is None or author_user_id != user.id:
        return False
    return not (comment.is_forwarded or is_routed(comment.issue))


def provider_attachment_backend_name(provider_helpdesk) -> str:
    """The ``backend_name`` of attachments held by a provider helpdesk's desk.

    A remote desk numbers attachments from its own sequence, so two providers
    on Jira, or a provider and the operator, hand out the same ids, and
    ``(backend_name, backend_id)`` is unique. A name per helpdesk keeps them
    apart.
    """
    return f"{provider_helpdesk.backend_type}:{provider_helpdesk.uuid.hex}"


def provider_settings_override(settings_dict, connection_settings):
    """The settings a provider helpdesk's backend reads before the operator's.

    A provider backend takes every setting the helpdesk leaves out from the
    operator's configuration. That fallback must not cover the settings that
    say where the backend connects and with which credentials, the
    ``connection_settings``, one at a time: a helpdesk that named only its
    own URL would have the operator's credentials sent there.

    So a helpdesk connects either with the operator's connection settings,
    all of them, or with its own alone. Once it gives any of them a value,
    the ones it leaves out are blank rather than the operator's.
    """
    if not any(settings_dict.get(key) for key in connection_settings):
        return settings_dict
    return {**dict.fromkeys(connection_settings, ""), **settings_dict}


def get_backend_for_provider(provider_helpdesk) -> SupportBackend:
    """Factory to create a backend instance for a given ProviderHelpdesk.

    For each backend type, creates a provider-scoped backend using the
    provider's settings dict. If settings are empty/incomplete, backends
    fall back to global Constance settings — so a provider with no custom
    settings effectively uses the operator's global backend config. The
    connection settings, its URL and credentials, fall back only as a whole;
    see `provider_settings_override`.
    """
    backend_type = provider_helpdesk.backend_type
    settings_dict = provider_helpdesk.settings or {}

    if backend_type == "basic":
        from .basic import BasicBackend

        instance = BasicBackend.from_settings(settings_dict)
    elif backend_type == "email":
        from .email_backend import EmailSupportBackend

        instance = EmailSupportBackend.from_settings(settings_dict, provider_helpdesk)
    elif backend_type == SupportBackendType.ATLASSIAN:
        from .atlassian import ServiceDeskBackend

        instance = ServiceDeskBackend.from_settings(settings_dict)
    elif backend_type == SupportBackendType.ZAMMAD:
        from .zammad import ZammadServiceBackend

        instance = ZammadServiceBackend.from_settings(settings_dict)
    elif backend_type == SupportBackendType.SMAX:
        from .smax import SmaxServiceBackend

        instance = SmaxServiceBackend.from_settings(settings_dict)
    else:
        raise SupportBackendError(f"Unknown provider backend type: {backend_type}")

    instance.attachment_backend_name = provider_attachment_backend_name(
        provider_helpdesk
    )
    return instance


def attachment_backend_name_for(issue) -> str | None:
    """The ``backend_name`` an attachment of ``issue`` is filed under, when
    it is not the operator's: that of the provider helpdesk the issue is
    routed to."""
    if issue.provider_helpdesk_id:
        return provider_attachment_backend_name(issue.provider_helpdesk)
    return None


def _remote_backend_class(backend_type):
    """The backend class of a remote desk type, or None for a desk in Waldur."""
    if backend_type == SupportBackendType.ATLASSIAN:
        from .atlassian import ServiceDeskBackend

        return ServiceDeskBackend
    if backend_type == SupportBackendType.ZAMMAD:
        from .zammad import ZammadServiceBackend

        return ZammadServiceBackend
    if backend_type == SupportBackendType.SMAX:
        from .smax import SmaxServiceBackend

        return SmaxServiceBackend
    return None


def _desk_value(key, value) -> str:
    """A desk setting as compared: a URL by scheme, host, port and path, so
    that the same server written differently is the same desk."""
    value = "" if value is None else str(value).strip()
    if not key.endswith("_URL") or not value:
        return value
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    port = parts.port
    if port == {"http": 80, "https": 443}.get(scheme):
        port = None
    host = parts.hostname or ""
    if ":" in host:
        # An IPv6 address keeps its brackets, or its last group would read
        # as a port.
        host = f"[{host}]"
    netloc = host + (f":{port}" if port else "")
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, ""))


def routed_elsewhere_q(backend_name) -> Q:
    """Tickets that live on a provider's desk rather than the operator's.

    The operator's sync and webhooks ask the operator's server, where a
    provider's ticket id names some other ticket. Those of a helpdesk of
    another type, or on a desk of its own, are left to it, and so are the
    children of a deleted helpdesk (``is_orphaned``), whose desk is not known.

    A helpdesk shares the operator's desk when ``get_backend_for_provider``
    would connect it to the same one: its ``desk_settings``, read through
    ``provider_settings_override`` with the operator's settings as the
    fallback, match the operator's. Computed from the settings alone, with
    the operator's read once. A helpdesk whose settings cannot be read counts
    as elsewhere.
    """
    other_types = Q(provider_helpdesk__isnull=False) & ~Q(
        provider_helpdesk__backend_type=backend_name
    )
    orphaned = Q(parent_issue__isnull=False, provider_helpdesk__isnull=True)
    helpdesks = models.ProviderHelpdesk.objects.filter(
        backend_type=backend_name
    ).values_list("id", "settings")
    desk_class = _remote_backend_class(backend_name)
    if desk_class is None:
        # No server to ask: such tickets live in Waldur.
        return other_types | orphaned

    keys = desk_class.desk_settings
    operator = {key: getattr(config, key, None) for key in keys}
    operator_desk = tuple(_desk_value(key, operator[key]) for key in keys)
    elsewhere = []
    for helpdesk_id, settings_dict in helpdesks:
        try:
            override = provider_settings_override(
                settings_dict or {}, desk_class.connection_settings
            )
            desk = tuple(
                _desk_value(key, override[key] if key in override else operator[key])
                for key in keys
            )
        except Exception:
            logger.warning(
                "Cannot tell which desk provider helpdesk %s uses; leaving its "
                "tickets out of the operator's sync.",
                helpdesk_id,
                exc_info=True,
            )
            desk = None
        if desk != operator_desk:
            elsewhere.append(helpdesk_id)
    return other_types | orphaned | Q(provider_helpdesk_id__in=elsewhere)


def operator_desk_issues(backend_name):
    """The tickets the operator's own desk of this type holds."""
    return models.Issue.objects.filter(backend_name=backend_name).exclude(
        routed_elsewhere_q(backend_name)
    )


def synced_attachments(issue, backend_name):
    """The attachments of ``issue`` a sync of its desk may delete as stale.

    Those filed under the desk's name or under the provider helpdesk's,
    whichever backend runs the sync: a provider helpdesk with no settings of
    its own shares the operator's desk, and the operator's sync then syncs the
    provider's tickets too. A copy still being pushed is not on the desk yet,
    and is left alone.
    """
    names = {backend_name, attachment_backend_name_for(issue)} - {None}
    return issue.attachments.filter(backend_name__in=names).exclude(
        models.forwarding_q()
    )


def is_orphaned(issue) -> bool:
    """A child whose provider helpdesk has been deleted.

    Its desk is not known any more, and the operator's desk, where its id
    names some other ticket, is not it.
    """
    return issue.parent_issue_id is not None and issue.provider_helpdesk_id is None


def get_backend_for_issue(issue) -> "SupportBackend":
    """Resolve the backend that owns an issue.

    A ticket routed to a provider helpdesk is handled by that provider's own
    backend (often the built-in basic desk), not the operator's globally
    configured one. Everything else -- operator tickets and the parents of
    routed ones -- goes to the active backend. Raises for a child whose
    helpdesk has been deleted (``is_orphaned``), as nothing may reach a desk
    on its behalf.
    """
    if issue.provider_helpdesk_id:
        return get_backend_for_provider(issue.provider_helpdesk)
    if is_orphaned(issue):
        raise SupportBackendError(
            f"Issue {issue.uuid.hex} was routed to a provider helpdesk that has "
            "been deleted, so its desk is not known."
        )
    return get_active_backend()


def find_backend_for_issue(issue) -> "SupportBackend | None":
    """``get_backend_for_issue``, or None for a child of a deleted helpdesk."""
    if is_orphaned(issue):
        return None
    return get_backend_for_issue(issue)
