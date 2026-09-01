import logging
import re
from typing import cast

from constance import config
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core import validators
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMIntegerField
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core import models as core_models
from waldur_core.core.enums import CoreStates
from waldur_core.core.validators import validate_name, validate_template_syntax
from waldur_core.media import models as media_models
from waldur_core.structure import models as structure_models

from . import managers
from .enums import ISSUE_STATUS_TYPE_CHOICES, IssueStatusTypes

logger = logging.getLogger(__name__)


class BackendNameMixin(models.Model):
    backend_name = models.CharField(max_length=255, blank=True, null=True, default=None)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if not self.backend_name:
            self.backend_name = config.WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE
            super().save(update_fields=["backend_name"])

    class Meta:
        abstract = True


class Issue(
    BackendNameMixin,
    core_models.UuidMixin,
    structure_models.StructureLoggableMixin,
    core_models.BackendModelMixin,
    TimeStampedModel,
    core_models.StateMixin,
    core_models.AvailableMixin,
):
    comments: models.Manager["Comment"]
    attachments: models.Manager["Attachment"]

    class Meta:
        ordering = ["-created", "id"]
        unique_together = ("backend_name", "backend_id")

    class Permissions:
        customer_path = "customer"
        project_path = "project"

    backend_id = models.CharField(max_length=255, blank=True, null=True)
    remote_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    key = models.CharField(max_length=255, blank=True)
    type = models.CharField(max_length=255)
    link = models.URLField(
        max_length=255, help_text=_("Link to issue in support system."), blank=True
    )

    summary = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    deadline = models.DateTimeField(blank=True, null=True)
    impact = models.CharField(max_length=255, blank=True)

    status = models.CharField(max_length=255)
    resolution = models.CharField(max_length=255, blank=True)
    priority = models.CharField(max_length=255, blank=True)

    caller = models.ForeignKey[core_models.User](
        settings.AUTH_USER_MODEL,
        related_name="created_issues",
        blank=True,
        null=True,
        help_text=_("Waldur user who has reported the issue."),
        on_delete=models.SET_NULL,
    )
    reporter = models.ForeignKey["SupportUser"](
        "SupportUser",
        related_name="reported_issues",
        blank=True,
        null=True,
        help_text=_(
            "Help desk user who have created the issue that is reported by caller."
        ),
        on_delete=models.PROTECT,
    )
    assignee = models.ForeignKey["SupportUser"](
        "SupportUser",
        related_name="issues",
        blank=True,
        null=True,
        help_text=_("Help desk user who will implement the issue"),
        on_delete=models.PROTECT,
    )

    customer = models.ForeignKey(
        structure_models.Customer,
        verbose_name=_("organization"),
        related_name="issues",
        blank=True,
        null=True,
        on_delete=models.CASCADE,
    )
    project = models.ForeignKey(
        structure_models.Project,
        related_name="issues",
        blank=True,
        null=True,
        on_delete=models.CASCADE,
    )

    resource_content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ContentType, null=True
    )
    resource_object_id = models.PositiveIntegerField(null=True)
    resource = GenericForeignKey("resource_content_type", "resource_object_id")

    offering = models.ForeignKey(
        "marketplace.Offering",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text=_(
            "Marketplace offering the issue is about. Determines the provider "
            "helpdesk to route to when no resource is attached."
        ),
    )

    resolution_date = models.DateTimeField(blank=True, null=True)
    template = models.ForeignKey["Template"](
        "Template",
        related_name="issues",
        blank=True,
        null=True,
        on_delete=models.PROTECT,
    )
    feedback_request = models.BooleanField(
        blank=True,
        default=True,
        help_text="Request feedback from the issue creator after resolution of the issue",
    )
    processing_log = models.JSONField(
        default=list,
        blank=True,
        help_text="Internal processing log for debugging order lifecycle events. Visible only to staff.",
    )

    # Routing fields
    parent_issue = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        related_name="child_issues",
        on_delete=models.SET_NULL,
        help_text=_("Parent issue if this is a provider-routed child issue."),
    )
    provider_helpdesk = models.ForeignKey(
        "ProviderHelpdesk",
        null=True,
        blank=True,
        related_name="issues",
        on_delete=models.SET_NULL,
        help_text=_("Provider helpdesk this issue was routed to."),
    )
    is_escalated = models.BooleanField(
        default=False,
        help_text=_("Whether this issue has been escalated."),
    )
    escalated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the issue was escalated."),
    )
    escalation_reason = models.TextField(
        blank=True,
        help_text=_("Reason for escalation."),
    )
    provider_assignee = models.ForeignKey(
        "ProviderSupportUser",
        null=True,
        blank=True,
        related_name="assigned_issues",
        on_delete=models.SET_NULL,
        help_text=_("Provider support user assigned to this issue."),
    )

    tags = models.ManyToManyField(
        "IssueTag",
        blank=True,
        related_name="issues",
    )

    # SLA fields
    first_response_deadline = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Deadline for first response from support staff."),
    )
    resolution_deadline = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Deadline for issue resolution."),
    )
    first_response_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Timestamp of first response from support staff."),
    )
    sla_breached = models.BooleanField(
        default=False,
        help_text=_("Whether SLA has been breached for this issue."),
    )

    objects = managers.IssueManager()

    tracker = cast(FieldInstanceTracker, FieldTracker())

    @property
    def safe_resource(self):
        """Return ``self.resource`` while tolerating stale GenericForeignKey
        targets. When ``resource_content_type`` points to a model that is no
        longer registered (e.g. its app was removed) ``ContentType.model_class()``
        returns ``None`` and Django's GFK descriptor raises
        ``AttributeError("'NoneType' object has no attribute '_base_manager'")``.
        We treat such issues as "no resource attached".
        """
        if self.resource_content_type_id is None:
            return None
        if self.resource_content_type.model_class() is None:
            return None
        try:
            return self.resource
        except AttributeError:
            return None

    def get_description(self):
        return self.description

    @classmethod
    def get_url_name(cls):
        return "support-issue"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "backend_id",
            "key",
            "type",
            "link",
            "summary",
            "description",
            "deadline",
            "impact",
            "status",
            "resolution",
            "priority",
            "caller",
            "reporter",
            "assignee",
            "customer",
            "project",
            "resource",
        )

    def get_log_fields(self):
        return (
            "uuid",
            "type",
            "key",
            "status",
            "link",
            "summary",
            "reporter",
            "caller",
            "customer",
            "project",
            "resource",
        )

    @property
    def resolved(self) -> bool | None:
        return IssueStatus.check_success_status(self.status)

    def set_resolved(self):
        self.status = (
            IssueStatus.objects.filter(type=IssueStatus.Types.RESOLVED).first().name
        )
        self.state = CoreStates.OK
        self.sync_resolution_date()
        self.save()

    def set_canceled(self):
        self.status = (
            IssueStatus.objects.filter(type=IssueStatus.Types.CANCELED).first().name
        )
        self.state = CoreStates.OK
        self.sync_resolution_date()
        self.save()

    def sync_resolution_date(self):
        """Keep `resolution_date` in step with the current status.

        Everything that reports on a closed ticket keys off this field rather
        than off the status name: the SLA badge only reads "met" once it is set,
        and the support statistics count an issue as open until it is. Leaving
        it null is what kept resolved tickets showing "On track" forever.

        It has to be cleared again when a ticket leaves a terminal status, or a
        reopened ticket would stay out of `Issue.objects.open()` for good and go
        on reporting a met SLA while its resolution deadline passes.

        Returns True when the field changed, so callers doing a partial save can
        decide whether to include it.
        """
        is_terminal = IssueStatus.check_success_status(self.status) is not None
        if is_terminal and self.resolution_date is None:
            self.resolution_date = timezone.now()
            return True
        if not is_terminal and self.resolution_date is not None:
            self.resolution_date = None
            return True
        return False

    def append_processing_log(self, event: str, details: dict | None = None):
        """
        Append an entry to the processing log for debugging.

        Args:
            event: Event type (e.g., "status_changed", "callback_invoked", "condition_failed")
            details: Optional dictionary with event-specific details
        """
        entry = {
            "timestamp": timezone.now().isoformat(),
            "event": event,
        }
        if details:
            entry["details"] = details

        if self.processing_log is None:
            self.processing_log = []
        self.processing_log.append(entry)

    def __str__(self):
        return "{}: {}".format(self.key or "???", self.summary)


class Priority(
    BackendNameMixin,
    core_models.NameMixin,
    core_models.UuidMixin,
    core_models.UiDescribableMixin,
):
    backend_id = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = _("Priority")
        verbose_name_plural = _("Priorities")

    @classmethod
    def get_url_name(cls):
        return "support-priority"

    def __str__(self):
        return self.name


class SupportUser(
    BackendNameMixin,
    core_models.UuidMixin,
    core_models.NameMixin,
    models.Model,
):
    reported_issues: models.Manager["Issue"]
    issues: models.Manager["Issue"]
    comments: models.Manager["Comment"]
    attachments: models.Manager["Attachment"]

    class Meta:
        ordering = ["name", "id"]
        unique_together = ("backend_name", "backend_id", "user")

    user = models.ForeignKey[core_models.User](
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name="+",
        blank=True,
        null=True,
    )
    backend_id = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_(
            "Designates whether this user should be treated as "
            "active. Unselect this instead of deleting accounts."
        ),
    )
    objects = managers.SupportUserManager()

    @classmethod
    def get_url_name(cls):
        return "support-user"

    def __str__(self):
        return self.name


class Comment(
    BackendNameMixin,
    core_models.UuidMixin,
    core_models.BackendModelMixin,
    TimeStampedModel,
    core_models.StateMixin,
):
    class Meta:
        ordering = ["-created", "id"]
        unique_together = ("backend_name", "backend_id")

    class Permissions:
        customer_path = "issue__customer"
        project_path = "issue__project"

    issue = models.ForeignKey(
        on_delete=models.CASCADE, to=Issue, related_name="comments"
    )
    author = models.ForeignKey(
        on_delete=models.CASCADE, to=SupportUser, related_name="comments"
    )
    description = models.TextField()
    is_public = models.BooleanField(default=True)
    is_forwarded = models.BooleanField(
        default=False,
        help_text=_("Whether this comment was forwarded from a parent/child issue."),
    )
    backend_id = models.CharField(max_length=255, blank=True, null=True)
    remote_id = models.CharField(max_length=255, blank=True, null=True)
    tracker = cast(FieldInstanceTracker, FieldTracker())

    def clean_message(self, message):
        """
        Extracts comment message from JIRA comment which contains user's info in its body.
        """
        match = re.search(r"^(\[.*?\]\:\s)", message)
        return message.replace(match.group(0), "") if match else message

    def prepare_message(self):
        """
        Prepends user info to the comment description to display comment author in JIRA.
        User info format - '[user.full_name user.civil_number]: '.
        """
        prefix = self.author.name
        # User is optional
        user = self.author.user
        if user:
            prefix = user.full_name or user.username
            if user.civil_number:
                prefix += " " + user.civil_number
        return f"[{prefix}]: {self.description}"

    def update_message(self, message):
        self.description = self.clean_message(message)

    @classmethod
    def get_url_name(cls):
        return "support-comment"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "issue",
            "author",
            "description",
            "is_public",
            "backend_id",
        )

    def __str__(self):
        return self.description[:50]


class FileMixin:
    """Mixin to provide file-related functionality and properties."""

    @property
    def file_size(self) -> int:
        if self.file:
            return (
                media_models.File.objects.filter(name=self.file.name)
                .only("size")
                .get()
                .size
            )

    @property
    def mime_type(self) -> str:
        if self.file:
            return (
                media_models.File.objects.filter(name=self.file.name)
                .only("mime_type")
                .get()
                .mime_type
            )


class Attachment(
    FileMixin,
    BackendNameMixin,
    core_models.UuidMixin,
    TimeStampedModel,
    structure_models.StructureLoggableMixin,
    core_models.StateMixin,
):
    class Permissions:
        customer_path = "issue__customer"
        project_path = "issue__project"

    class Meta:
        unique_together = ("backend_name", "backend_id")
        ordering = ["created", "id"]

    issue = models.ForeignKey(
        on_delete=models.CASCADE, to=Issue, related_name="attachments"
    )
    file = models.FileField(upload_to="support_attachments")
    backend_id = models.CharField(max_length=255)
    author = models.ForeignKey(
        on_delete=models.CASCADE,
        to=SupportUser,
        related_name="attachments",
        blank=True,
        null=True,
    )
    objects = managers.AttachmentManager()

    @classmethod
    def get_url_name(cls):
        return "support-attachment"

    def __str__(self):
        return "{} | {}".format(self.issue, self.file.name.split("/")[-1])

    def get_log_fields(self):
        return ("uuid", "issue", "author", "backend_id")


class Template(core_models.UuidMixin, core_models.NameMixin, TimeStampedModel):
    issues: models.Manager["Issue"]
    attachments: models.Manager["TemplateAttachment"]

    class IssueTypes:
        INFORMATIONAL = "INFORMATIONAL"
        SERVICE_REQUEST = "SERVICE_REQUEST"
        CHANGE_REQUEST = "CHANGE_REQUEST"
        INCIDENT = "INCIDENT"

        CHOICES = (
            (INFORMATIONAL, "Informational"),
            (SERVICE_REQUEST, "Service request"),
            (CHANGE_REQUEST, "Change request"),
            (INCIDENT, "Incident"),
        )

    description = models.TextField()
    issue_type = models.CharField(
        max_length=30, choices=IssueTypes.CHOICES, default=IssueTypes.INFORMATIONAL
    )

    @classmethod
    def get_url_name(cls):
        return "support-template"

    def __str__(self):
        return self.name


class TemplateAttachment(
    FileMixin, core_models.UuidMixin, core_models.NameMixin, TimeStampedModel
):
    template = models.ForeignKey(
        Template, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(upload_to="support_template_attachments")


class IgnoredIssueStatus(models.Model):
    name = models.CharField(
        _("name"), max_length=150, validators=[validate_name], unique=True
    )

    def __str__(self):
        return self.name


class TemplateStatusNotification(models.Model):
    status = models.CharField(max_length=255, validators=[validate_name], unique=True)
    html = models.TextField(validators=[validate_name, validate_template_syntax])
    text = models.TextField(validators=[validate_name, validate_template_syntax])
    subject = models.CharField(
        max_length=255, validators=[validate_name, validate_template_syntax]
    )

    def __str__(self):
        return self.status


class SupportCustomer(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    backend_id = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return self.user.full_name


class RequestType(
    BackendNameMixin, core_models.UuidMixin, core_models.NameMixin, models.Model
):
    backend_id = models.IntegerField(
        unique=True,
        null=True,
        blank=True,
        help_text="Backend ID for synced types. Null for manually created types.",
    )
    issue_type_name = models.CharField(max_length=255)
    fields = models.JSONField(
        default=dict,
        blank=True,
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this request type is available for issue creation.",
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="Display order. First type (lowest order) is the default.",
    )

    class Meta:
        ordering = ["order", "name", "id"]

    def __str__(self):
        return self.name


class IssueStatus(core_models.UuidMixin, models.Model):
    """This model is needed in order to understand whether the issue has been solved or not.

    The field of resolution does not give an exact answer since may be the same in both cases.
    """

    Types = IssueStatusTypes  # Alias for backwards compatibility

    name = models.CharField(
        max_length=255, help_text="Status name in Jira.", unique=True
    )
    type = FSMIntegerField(
        default=IssueStatusTypes.RESOLVED, choices=ISSUE_STATUS_TYPE_CHOICES
    )

    @classmethod
    def check_success_status(cls, status):
        """Check an issue has been resolved.

        True if an issue resolved.
        False if an issue canceled.
        None in all other cases.
        """
        logger.debug("Checking success status for: %s", status)

        has_resolved = cls.objects.filter(type=cls.Types.RESOLVED).exists()
        has_canceled = cls.objects.filter(type=cls.Types.CANCELED).exists()

        if not has_resolved or not has_canceled:
            available_statuses = list(
                cls.objects.values_list("name", "type").order_by("name")
            )
            logger.critical(
                "IssueStatus configuration incomplete. "
                "RESOLVED type exists: %s, CANCELED type exists: %s. "
                "Available statuses: %s. "
                "Please add both RESOLVED and CANCELED status types in admin.",
                has_resolved,
                has_canceled,
                available_statuses,
            )
            return None

        try:
            issue_status = cls.objects.get(name=status)
            logger.debug(
                "Found IssueStatus: name=%s, type=%s (%s)",
                issue_status.name,
                issue_status.type,
                "RESOLVED" if issue_status.type == cls.Types.RESOLVED else "CANCELED",
            )
            if issue_status.type == cls.Types.RESOLVED:
                return True
            if issue_status.type == cls.Types.CANCELED:
                return False
            # Status exists but has unexpected type
            logger.warning(
                "IssueStatus '%s' has unexpected type: %s",
                status,
                issue_status.type,
            )
            return None
        except cls.DoesNotExist:
            available_statuses = list(
                cls.objects.values_list("name", flat=True).order_by("name")
            )
            logger.warning(
                "IssueStatus not found for status name: '%s'. "
                "Available status names: %s",
                status,
                available_statuses,
            )
            return None

    class Meta:
        verbose_name = _("Issue status")
        verbose_name_plural = _("Issue statuses")

    def __str__(self):
        return f"{self.name} / {self.type}"


class TemplateConfirmationComment(models.Model):
    """
    This model allows to automate adding a custom announcement to the user
    that his ticket has been received and worked on.

    Default text is to be used for all requests.
    Issue type specific text template is to be used for incident, etc.
    """

    issue_type = models.CharField(max_length=255, unique=True, default="default")
    template = models.TextField(validators=[validate_name, validate_template_syntax])

    def __str__(self):
        return self.issue_type


class Feedback(
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.StateMixin,
):
    issue = models.OneToOneField(Issue, on_delete=models.CASCADE)
    evaluation = models.SmallIntegerField(
        validators=[
            validators.MinValueValidator(1),
            validators.MaxValueValidator(10),
        ]
    )
    comment = models.TextField(blank=True)

    def __str__(self):
        return f"{self.issue} | {self.evaluation}"

    @classmethod
    def get_url_name(cls):
        return "support-feedback"


class IssueStatusTransition(core_models.UuidMixin, models.Model):
    """Defines allowed status transitions for issues.

    When populated, only transitions listed here are permitted.
    When empty, all transitions are allowed (backwards compatibility).
    """

    from_status = models.CharField(max_length=255)
    to_status = models.CharField(max_length=255)

    class Meta:
        unique_together = ("from_status", "to_status")
        ordering = ["from_status", "to_status", "id"]

    @classmethod
    def is_transition_allowed(cls, from_status, to_status):
        """Check if a transition is allowed.

        If no transitions are defined, all transitions are allowed.
        """
        if not cls.objects.exists():
            return True
        return cls.objects.filter(from_status=from_status, to_status=to_status).exists()

    def __str__(self):
        return f"{self.from_status} -> {self.to_status}"


class ProviderHelpdesk(
    core_models.UuidMixin,
    TimeStampedModel,
):
    class BackendTypes:
        BASIC = "basic"
        EMAIL = "email"
        ATLASSIAN = "atlassian"
        ZAMMAD = "zammad"
        SMAX = "smax"

        CHOICES = (
            (BASIC, "Basic (built-in)"),
            (EMAIL, "Email"),
            (ATLASSIAN, "Atlassian JIRA"),
            (ZAMMAD, "Zammad"),
            (SMAX, "SMAX"),
        )

    service_provider = models.OneToOneField(
        "marketplace.ServiceProvider",
        on_delete=models.CASCADE,
        related_name="provider_helpdesk",
    )
    backend_type = models.CharField(
        max_length=50,
        choices=BackendTypes.CHOICES,
        default=BackendTypes.BASIC,
    )
    settings = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Backend-specific configuration."),
    )
    is_active = models.BooleanField(default=True)
    webhook_secret = models.CharField(max_length=255, blank=True)
    notification_email = models.EmailField(
        blank=True,
        help_text=_("Primary email for notifications."),
    )
    notify_on_new_ticket = models.BooleanField(default=True)
    notify_on_comment = models.BooleanField(default=True)
    notify_on_escalation = models.BooleanField(default=True)
    notify_on_sla_warning = models.BooleanField(default=True)

    # Monitoring fields
    last_health_check = models.DateTimeField(null=True, blank=True)
    last_health_status = models.CharField(max_length=50, blank=True)
    failed_routing_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created", "id"]
        verbose_name = _("Provider helpdesk")
        verbose_name_plural = _("Provider helpdesks")

    def get_notification_emails(self):
        emails = []
        if self.notification_email:
            emails.append(self.notification_email)
        # Also include service provider lead email
        if self.service_provider.lead_email:
            emails.append(self.service_provider.lead_email)
        return list(set(emails))

    @property
    def health_status(self):
        if not self.last_health_check:
            return "unknown"
        return self.last_health_status or "unknown"

    @classmethod
    def get_url_name(cls):
        return "provider-helpdesk"

    def __str__(self):
        return f"Helpdesk for {self.service_provider}"


class ProviderSupportUser(core_models.UuidMixin, TimeStampedModel):
    class Roles:
        AGENT = "agent"
        MANAGER = "manager"

        CHOICES = (
            (AGENT, "Agent"),
            (MANAGER, "Manager"),
        )

    provider_helpdesk = models.ForeignKey(
        ProviderHelpdesk,
        on_delete=models.CASCADE,
        related_name="support_users",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="provider_support_roles",
    )
    role = models.CharField(
        max_length=50,
        choices=Roles.CHOICES,
        default=Roles.AGENT,
    )
    is_active = models.BooleanField(default=True)
    skills = models.JSONField(
        default=list,
        blank=True,
        help_text=_("List of skill tags for routing."),
    )
    max_open_tickets = models.PositiveIntegerField(
        default=20,
        help_text=_("Maximum number of open tickets this user can handle."),
    )

    class Meta:
        unique_together = ("provider_helpdesk", "user")
        ordering = ["user__first_name", "user__last_name", "id"]

    @property
    def open_ticket_count(self):
        return Issue.objects.filter(
            provider_assignee=self,
            resolution_date__isnull=True,
        ).count()

    @property
    def has_capacity(self):
        return self.open_ticket_count < self.max_open_tickets

    @classmethod
    def get_url_name(cls):
        return "provider-support-user"

    def __str__(self):
        return f"{self.user.full_name} ({self.provider_helpdesk})"


class ProviderCannedResponse(
    core_models.UuidMixin,
    core_models.NameMixin,
    TimeStampedModel,
):
    provider_helpdesk = models.ForeignKey(
        ProviderHelpdesk,
        on_delete=models.CASCADE,
        related_name="canned_responses",
    )
    text = models.TextField(
        help_text=_("Template text. Supports Django template syntax.")
    )
    category = models.CharField(max_length=255, blank=True)
    usage_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-usage_count", "name", "id"]

    def render(self, context_dict=None):
        from django.template import Context, Template

        template = Template(self.text)
        context = Context(context_dict or {})
        return template.render(context)

    @classmethod
    def get_url_name(cls):
        return "provider-canned-response"

    def __str__(self):
        return self.name


class IssueTag(core_models.UuidMixin, core_models.NameMixin, models.Model):
    color = models.CharField(
        max_length=7,
        blank=True,
        help_text=_("Hex color code, e.g. #FF0000."),
    )

    class Meta:
        ordering = ["name", "id"]

    @classmethod
    def get_url_name(cls):
        return "support-issue-tag"

    def __str__(self):
        return self.name


class IssueLink(core_models.UuidMixin, models.Model):
    class LinkTypes:
        RELATED = "related"
        BLOCKED_BY = "blocked_by"
        DUPLICATES = "duplicates"

        CHOICES = (
            (RELATED, "Related"),
            (BLOCKED_BY, "Blocked by"),
            (DUPLICATES, "Duplicates"),
        )

    source = models.ForeignKey(
        Issue,
        on_delete=models.CASCADE,
        related_name="outgoing_links",
    )
    target = models.ForeignKey(
        Issue,
        on_delete=models.CASCADE,
        related_name="incoming_links",
    )
    link_type = models.CharField(
        max_length=50,
        choices=LinkTypes.CHOICES,
        default=LinkTypes.RELATED,
    )

    class Meta:
        unique_together = ("source", "target")
        ordering = ["source", "target", "id"]

    @classmethod
    def get_url_name(cls):
        return "support-issue-link"

    def __str__(self):
        return f"{self.source.key} -> {self.target.key} ({self.link_type})"


class SavedFilter(core_models.UuidMixin, core_models.NameMixin, TimeStampedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saved_filters",
    )
    filter_params = models.JSONField(
        default=dict,
        help_text=_("Saved filter parameters as JSON."),
    )
    is_shared = models.BooleanField(
        default=False,
        help_text=_("If True, visible to all staff/support users."),
    )

    class Meta:
        ordering = ["-modified", "id"]

    @classmethod
    def get_url_name(cls):
        return "support-saved-filter"

    def __str__(self):
        return self.name


class CannedResponse(
    core_models.UuidMixin,
    core_models.NameMixin,
    TimeStampedModel,
):
    text = models.TextField(
        help_text=_("Template text. Supports Django template syntax.")
    )
    category = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["category", "name", "id"]

    def render(self, context_dict=None):
        from django.template import Context, Template

        template = Template(self.text)
        context = Context(context_dict or {})
        return template.render(context)

    @classmethod
    def get_url_name(cls):
        return "support-canned-response"

    def __str__(self):
        return self.name
