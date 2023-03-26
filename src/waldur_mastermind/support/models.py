import logging
import re

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMIntegerField
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.core.validators import validate_name, validate_template_syntax
from waldur_core.structure import models as structure_models

from . import managers

logger = logging.getLogger(__name__)


class BackendNameMixin(models.Model):
    backend_name = models.CharField(max_length=255, blank=True, null=True, default=None)

    class Meta:
        abstract = True


class Issue(
    BackendNameMixin,
    core_models.UuidMixin,
    structure_models.StructureLoggableMixin,
    core_models.BackendModelMixin,
    TimeStampedModel,
    core_models.StateMixin,
):
    class Meta:
        ordering = ['-created']
        unique_together = ('backend_name', 'backend_id')

    class Permissions:
        customer_path = 'customer'
        project_path = 'project'

    backend_id = models.CharField(max_length=255, blank=True, null=True)
    remote_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    key = models.CharField(max_length=255, blank=True)
    type = models.CharField(max_length=255)
    link = models.URLField(
        max_length=255, help_text=_('Link to issue in support system.'), blank=True
    )

    summary = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    deadline = models.DateTimeField(blank=True, null=True)
    impact = models.CharField(max_length=255, blank=True)

    status = models.CharField(max_length=255)
    resolution = models.CharField(max_length=255, blank=True)
    priority = models.CharField(max_length=255, blank=True)

    caller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='created_issues',
        blank=True,
        null=True,
        help_text=_('Waldur user who has reported the issue.'),
        on_delete=models.SET_NULL,
    )
    reporter = models.ForeignKey(
        'SupportUser',
        related_name='reported_issues',
        blank=True,
        null=True,
        help_text=_(
            'Help desk user who have created the issue that is reported by caller.'
        ),
        on_delete=models.PROTECT,
    )
    assignee = models.ForeignKey(
        'SupportUser',
        related_name='issues',
        blank=True,
        null=True,
        help_text=_('Help desk user who will implement the issue'),
        on_delete=models.PROTECT,
    )

    customer = models.ForeignKey(
        structure_models.Customer,
        verbose_name=_('organization'),
        related_name='issues',
        blank=True,
        null=True,
        on_delete=models.CASCADE,
    )
    project = models.ForeignKey(
        structure_models.Project,
        related_name='issues',
        blank=True,
        null=True,
        on_delete=models.CASCADE,
    )

    resource_content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ContentType, null=True
    )
    resource_object_id = models.PositiveIntegerField(null=True)
    resource = GenericForeignKey('resource_content_type', 'resource_object_id')

    first_response_sla = models.DateTimeField(blank=True, null=True)
    resolution_date = models.DateTimeField(blank=True, null=True)
    template = models.ForeignKey(
        'Template',
        related_name='issues',
        blank=True,
        null=True,
        on_delete=models.PROTECT,
    )
    feedback_request = models.BooleanField(
        blank=True,
        default=True,
        help_text='Request feedback from the issue creator after resolution of the issue',
    )

    tracker = FieldTracker()

    def get_description(self):
        return self.description

    @classmethod
    def get_url_name(cls):
        return 'support-issue'

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            'backend_id',
            'key',
            'type',
            'link',
            'summary',
            'description',
            'deadline',
            'impact',
            'status',
            'resolution',
            'priority',
            'caller',
            'reporter',
            'assignee',
            'customer',
            'project',
            'resource',
            'first_response_sla',
        )

    def get_log_fields(self):
        return (
            'uuid',
            'type',
            'key',
            'status',
            'link',
            'summary',
            'reporter',
            'caller',
            'customer',
            'project',
            'resource',
        )

    @property
    def resolved(self):
        return IssueStatus.check_success_status(self.status)

    def set_resolved(self):
        self.status = (
            IssueStatus.objects.filter(type=IssueStatus.Types.RESOLVED).first().name
        )
        self.state = Issue.States.OK
        self.save()

    def set_canceled(self):
        self.status = (
            IssueStatus.objects.filter(type=IssueStatus.Types.CANCELED).first().name
        )
        self.state = Issue.States.OK
        self.save()

    def __str__(self):
        return '{}: {}'.format(self.key or '???', self.summary)


class Priority(
    core_models.NameMixin, core_models.UuidMixin, core_models.UiDescribableMixin
):
    backend_id = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = _('Priority')
        verbose_name_plural = _('Priorities')

    @classmethod
    def get_url_name(cls):
        return 'support-priority'

    def __str__(self):
        return self.name


class SupportUser(
    BackendNameMixin,
    core_models.UuidMixin,
    core_models.NameMixin,
    models.Model,
):
    class Meta:
        ordering = ['name']
        unique_together = ('backend_name', 'backend_id')

    user = models.ForeignKey(
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name='+',
        blank=True,
        null=True,
    )
    backend_id = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(
        _('active'),
        default=True,
        help_text=_(
            'Designates whether this user should be treated as '
            'active. Unselect this instead of deleting accounts.'
        ),
    )
    objects = managers.SupportUserManager()

    @classmethod
    def get_url_name(cls):
        return 'support-user'

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
        ordering = ['-created']
        unique_together = ('backend_name', 'backend_id')

    class Permissions:
        customer_path = 'issue__customer'
        project_path = 'issue__project'

    issue = models.ForeignKey(
        on_delete=models.CASCADE, to=Issue, related_name='comments'
    )
    author = models.ForeignKey(
        on_delete=models.CASCADE, to=SupportUser, related_name='comments'
    )
    description = models.TextField()
    is_public = models.BooleanField(default=True)
    backend_id = models.CharField(max_length=255, blank=True, null=True)
    remote_id = models.CharField(max_length=255, blank=True, null=True)
    tracker = FieldTracker()

    def clean_message(self, message):
        """
        Extracts comment message from JIRA comment which contains user's info in its body.
        """
        match = re.search(r'^(\[.*?\]\:\s)', message)
        return message.replace(match.group(0), '') if match else message

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
                prefix += ' ' + user.civil_number
        return f'[{prefix}]: {self.description}'

    def update_message(self, message):
        self.description = self.clean_message(message)

    @classmethod
    def get_url_name(cls):
        return 'support-comment'

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            'issue',
            'author',
            'description',
            'is_public',
            'backend_id',
        )

    def __str__(self):
        return self.description[:50]


class Attachment(
    BackendNameMixin,
    core_models.UuidMixin,
    TimeStampedModel,
    structure_models.StructureLoggableMixin,
    core_models.StateMixin,
):
    class Permissions:
        customer_path = 'issue__customer'
        project_path = 'issue__project'

    class Meta:
        unique_together = ('backend_name', 'backend_id')

    issue = models.ForeignKey(
        on_delete=models.CASCADE, to=Issue, related_name='attachments'
    )
    file = models.FileField(upload_to='support_attachments')
    backend_id = models.CharField(max_length=255)
    mime_type = models.CharField(_('MIME type'), max_length=100, blank=True)
    file_size = models.PositiveIntegerField(_('Filesize, B'), blank=True, null=True)
    thumbnail = models.FileField(
        upload_to='support_attachments_thumbnails', blank=True, null=True
    )
    author = models.ForeignKey(
        on_delete=models.CASCADE,
        to=SupportUser,
        related_name='attachments',
        blank=True,
        null=True,
    )
    objects = managers.AttachmentManager()

    @classmethod
    def get_url_name(cls):
        return 'support-attachment'

    def __str__(self):
        return '{} | {}'.format(self.issue, self.file.name.split('/')[-1])

    def get_log_fields(self):
        return ('uuid', 'issue', 'author', 'backend_id')


class Template(core_models.UuidMixin, core_models.NameMixin, TimeStampedModel):
    class IssueTypes:
        INFORMATIONAL = 'INFORMATIONAL'
        SERVICE_REQUEST = 'SERVICE_REQUEST'
        CHANGE_REQUEST = 'CHANGE_REQUEST'
        INCIDENT = 'INCIDENT'

        CHOICES = (
            (INFORMATIONAL, 'Informational'),
            (SERVICE_REQUEST, 'Service request'),
            (CHANGE_REQUEST, 'Change request'),
            (INCIDENT, 'Incident'),
        )

    native_name = models.CharField(max_length=150, blank=True)
    description = models.TextField()
    native_description = models.TextField(blank=True)
    issue_type = models.CharField(
        max_length=30, choices=IssueTypes.CHOICES, default=IssueTypes.INFORMATIONAL
    )

    @classmethod
    def get_url_name(cls):
        return 'support-template'

    def __str__(self):
        return self.name


class TemplateAttachment(
    core_models.UuidMixin, core_models.NameMixin, TimeStampedModel
):
    template = models.ForeignKey(
        Template, on_delete=models.CASCADE, related_name='attachments'
    )
    file = models.FileField(upload_to='support_template_attachments')


class IgnoredIssueStatus(models.Model):
    name = models.CharField(
        _('name'), max_length=150, validators=[validate_name], unique=True
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


class RequestType(core_models.UuidMixin, core_models.NameMixin, models.Model):
    backend_id = models.IntegerField(unique=True)
    issue_type_name = models.CharField(max_length=255)
    fields = models.JSONField(
        default=dict,
        blank=True,
    )

    def __str__(self):
        return self.name


class IssueStatus(models.Model):
    """This model is needed in order to understand whether the issue has been solved or not.

    The field of resolution does not give an exact answer since may be the same in both cases.
    """

    class Types:
        RESOLVED = 0
        CANCELED = 1

    TYPE_CHOICES = (
        (Types.RESOLVED, 'Resolved'),
        (Types.CANCELED, 'Canceled'),
    )

    name = models.CharField(
        max_length=255, help_text='Status name in Jira.', unique=True
    )
    type = FSMIntegerField(default=Types.RESOLVED, choices=TYPE_CHOICES)

    @classmethod
    def check_success_status(cls, status):
        """Check an issue has been resolved.

        True if an issue resolved.
        False if an issue canceled.
        None in all other cases.
        """
        if (
            not cls.objects.filter(type=cls.Types.RESOLVED).exists()
            or not cls.objects.filter(type=cls.Types.CANCELED).exists()
        ):
            logger.critical(
                'There is no information about statuses of an issue. '
                'Please, add resolved and canceled statuses in admin. '
                'Otherwise, you cannot use processes that need this information.'
            )
            return
        try:
            issue_status = cls.objects.get(name=status)
            if issue_status.type == cls.Types.RESOLVED:
                return True
            if issue_status.type == cls.Types.CANCELED:
                return False
        except cls.DoesNotExist:
            return

    class Meta:
        verbose_name = _('Issue status')
        verbose_name_plural = _('Issue statuses')


class TemplateConfirmationComment(models.Model):
    """
    This model allows to automate adding a custom announcement to the user
    that his ticket has been received and worked on.

    Default text is to be used for all requests.
    Issue type specific text template is to be used for incident, etc.
    """

    issue_type = models.CharField(max_length=255, unique=True, default='default')
    template = models.TextField(validators=[validate_name, validate_template_syntax])

    def __str__(self):
        return self.issue_type


class Feedback(
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.StateMixin,
):
    class Evaluation:
        CHOICES = (
            (1, '1'),
            (2, '2'),
            (3, '3'),
            (4, '4'),
            (5, '5'),
            (6, '6'),
            (7, '7'),
            (8, '8'),
            (9, '9'),
            (10, '10'),
        )

    issue = models.OneToOneField(Issue, on_delete=models.CASCADE)
    evaluation = models.SmallIntegerField(choices=Evaluation.CHOICES)
    comment = models.TextField(blank=True)

    def __str__(self):
        return f'{self.issue} | {self.evaluation}'

    @classmethod
    def get_url_name(cls):
        return 'support-feedback'
