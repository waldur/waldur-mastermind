from __future__ import unicode_literals

import logging
import re

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import JSONField as BetterJSONField
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from django_fsm import FSMIntegerField
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel

from waldur_core.core import fields as core_fields
from waldur_core.core import models as core_models
from waldur_core.core.validators import validate_name
from waldur_core.structure import models as structure_models
from waldur_mastermind.common import mixins as common_mixins

from . import backend, managers

logger = logging.getLogger(__name__)


@python_2_unicode_compatible
class Issue(core_models.UuidMixin,
            structure_models.StructureLoggableMixin,
            core_models.BackendModelMixin,
            TimeStampedModel,
            core_models.StateMixin):
    class Meta:
        ordering = ['-created']

    class Permissions(object):
        customer_path = 'customer'
        project_path = 'project'

    backend_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    key = models.CharField(max_length=255, blank=True)
    type = models.CharField(max_length=255)
    link = models.URLField(max_length=255, help_text=_('Link to issue in support system.'), blank=True)

    summary = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    deadline = models.DateTimeField(blank=True, null=True)
    impact = models.CharField(max_length=255, blank=True)

    status = models.CharField(max_length=255)
    resolution = models.CharField(max_length=255, blank=True)
    priority = models.CharField(max_length=255, blank=True)

    caller = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='created_issues',
                               help_text=_('Waldur user who has reported the issue.'),
                               on_delete=models.PROTECT)
    reporter = models.ForeignKey('SupportUser', related_name='reported_issues', blank=True, null=True,
                                 help_text=_('Help desk user who have created the issue that is reported by caller.'),
                                 on_delete=models.PROTECT)
    assignee = models.ForeignKey('SupportUser', related_name='issues', blank=True, null=True,
                                 help_text=_('Help desk user who will implement the issue'),
                                 on_delete=models.PROTECT)

    customer = models.ForeignKey(structure_models.Customer, verbose_name=_('organization'),
                                 related_name='issues', blank=True, null=True,
                                 on_delete=models.CASCADE)
    project = models.ForeignKey(structure_models.Project, related_name='issues', blank=True, null=True,
                                on_delete=models.CASCADE)

    resource_content_type = models.ForeignKey(ContentType, null=True)
    resource_object_id = models.PositiveIntegerField(null=True)
    resource = GenericForeignKey('resource_content_type', 'resource_object_id')

    first_response_sla = models.DateTimeField(blank=True, null=True)
    resolution_date = models.DateTimeField(blank=True, null=True)
    template = models.ForeignKey('Template', related_name='issues', blank=True, null=True, on_delete=models.PROTECT)

    tracker = FieldTracker()

    def get_description(self):
        return self.description

    @classmethod
    def get_url_name(cls):
        return 'support-issue'

    @classmethod
    def get_backend_fields(cls):
        return super(Issue, cls).get_backend_fields() + ('backend_id', 'key', 'type', 'link',
                                                         'summary', 'description', 'deadline', 'impact',
                                                         'status', 'resolution', 'priority',
                                                         'caller', 'reporter', 'assignee', 'customer', 'project',
                                                         'resource', 'first_response_sla')

    def get_log_fields(self):
        return ('uuid', 'type', 'key', 'status', 'link', 'summary',
                'reporter', 'caller', 'customer', 'project', 'resource')

    @property
    def resolved(self):
        return IssueStatus.check_success_status(self.status)

    def set_resolved(self):
        self.status = IssueStatus.objects.filter(type=IssueStatus.Types.RESOLVED).first().name
        self.state = Issue.States.OK
        self.save()

    def set_canceled(self):
        self.status = IssueStatus.objects.filter(type=IssueStatus.Types.CANCELED).first().name
        self.state = Issue.States.OK
        self.save()

    def __str__(self):
        return '{}: {}'.format(self.key or '???', self.summary)


@python_2_unicode_compatible
class SupportUser(core_models.UuidMixin, core_models.NameMixin, models.Model):
    class Meta:
        ordering = ['name']

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='+', blank=True, null=True)
    backend_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    is_active = models.BooleanField(_('active'), default=True,
                                    help_text=_('Designates whether this user should be treated as '
                                                'active. Unselect this instead of deleting accounts.'))
    objects = managers.SupportUserManager()

    @classmethod
    def get_url_name(cls):
        return 'support-user'

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class Comment(core_models.UuidMixin,
              core_models.BackendModelMixin,
              TimeStampedModel,
              core_models.StateMixin):
    class Meta:
        ordering = ['-created']
        unique_together = ('backend_id', 'issue')

    class Permissions(object):
        customer_path = 'issue__customer'
        project_path = 'issue__project'

    issue = models.ForeignKey(Issue, related_name='comments')
    author = models.ForeignKey(SupportUser, related_name='comments')
    description = models.TextField()
    is_public = models.BooleanField(default=True)
    backend_id = models.CharField(max_length=255, blank=True, null=True)

    def clean_message(self, message):
        """
        Extracts comment message from JIRA comment which contains user's info in its body.
        """
        match = re.search('^(\[.*?\]\:\s)', message)
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
        return '[%s]: %s' % (prefix, self.description)

    def update_message(self, message):
        self.description = self.clean_message(message)

    @classmethod
    def get_url_name(cls):
        return 'support-comment'

    @classmethod
    def get_backend_fields(cls):
        return super(Comment, cls).get_backend_fields() + ('issue', 'author', 'description', 'is_public', 'backend_id')

    def __str__(self):
        return self.description[:50]


@python_2_unicode_compatible
class Offering(core_models.UuidMixin,
               core_models.NameMixin,
               common_mixins.ProductCodeMixin,
               common_mixins.UnitPriceMixin,
               structure_models.StructureLoggableMixin,
               TimeStampedModel):

    class Meta:
        ordering = ['-created']
        verbose_name = _('Request')
        verbose_name_plural = _('Requests')

    class Permissions(object):
        customer_path = 'project__customer'
        project_path = 'project'

    class States(object):
        REQUESTED = 'requested'
        OK = 'ok'
        TERMINATED = 'terminated'

        CHOICES = ((REQUESTED, _('Requested')), (OK, _('OK')), (TERMINATED, _('Terminated')))

    template = models.ForeignKey('OfferingTemplate', on_delete=models.PROTECT)
    plan = models.ForeignKey('OfferingPlan', blank=True, null=True, on_delete=models.PROTECT)
    issue = models.ForeignKey(Issue, null=True, on_delete=models.PROTECT)
    project = models.ForeignKey(structure_models.Project, null=True, on_delete=models.PROTECT)
    state = models.CharField(default=States.REQUESTED, max_length=30, choices=States.CHOICES)
    report = core_fields.JSONField(blank=True)
    terminated_at = models.DateTimeField(editable=False, blank=True, null=True)

    tracker = FieldTracker()

    def get_backend(self):
        backend.get_active_backend()

    def get_log_fields(self):
        return super(Offering, self).get_log_fields() + ('state', )

    def terminate(self):
        self.state = Offering.States.TERMINATED
        self.save()

    def set_ok(self):
        self.state = Offering.States.OK
        self.save()

    @property
    def type(self):
        return self.template.name

    @property
    def type_label(self):
        return self.template.config.get('label', None)

    @classmethod
    def get_url_name(cls):
        return 'support-offering'

    def __str__(self):
        return '{}: {}'.format(self.type_label or self.name, self.state)

    @classmethod
    def get_scope_type(cls):
        return 'Support.Offering'

    def _get_log_context(self, entity_name):
        context = super(Offering, self)._get_log_context(entity_name)
        context['resource_type'] = self.get_scope_type()
        context['resource_uuid'] = self.uuid.hex
        return context

    @property
    def config(self):
        return self.template.config if self.template else {}


@python_2_unicode_compatible
class OfferingTemplate(core_models.UuidMixin,
                       TimeStampedModel):
    name = models.CharField(_('name'), max_length=150)
    config = BetterJSONField()
    sort_order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ['sort_order', 'name']

    @classmethod
    def get_url_name(cls):
        return 'support-offering-template'

    def __str__(self):
        return self.name


class OfferingPlan(core_models.UuidMixin,
                   core_models.NameMixin,
                   core_models.DescribableMixin,
                   common_mixins.ProductCodeMixin,
                   common_mixins.UnitPriceMixin):
    template = models.ForeignKey(OfferingTemplate, related_name='plans')


@python_2_unicode_compatible
class Attachment(core_models.UuidMixin,
                 TimeStampedModel,
                 core_models.StateMixin):
    class Permissions(object):
        customer_path = 'issue__customer'
        project_path = 'issue__project'

    issue = models.ForeignKey(Issue, related_name='attachments')
    file = models.FileField(upload_to='support_attachments')
    backend_id = models.CharField(max_length=255, unique=True)
    mime_type = models.CharField(_('MIME type'), max_length=100, blank=True)
    file_size = models.PositiveIntegerField(_('Filesize, B'), blank=True, null=True)
    thumbnail = models.FileField(upload_to='support_attachments_thumbnails', blank=True, null=True)
    author = models.ForeignKey(SupportUser, related_name='attachments', blank=True, null=True)

    @classmethod
    def get_url_name(cls):
        return 'support-attachment'

    def __str__(self):
        return '{} | {}'.format(self.issue, self.file.name.split('/')[-1])


class Template(core_models.UuidMixin,
               core_models.NameMixin,
               TimeStampedModel):

    class IssueTypes(object):
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
    issue_type = models.CharField(max_length=30, choices=IssueTypes.CHOICES, default=IssueTypes.INFORMATIONAL)

    @classmethod
    def get_url_name(cls):
        return 'support-template'

    def __str__(self):
        return self.name


class TemplateAttachment(core_models.UuidMixin,
                         core_models.NameMixin,
                         TimeStampedModel):
    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='support_template_attachments')


class IgnoredIssueStatus(models.Model):
    name = models.CharField(_('name'), max_length=150, validators=[validate_name], unique=True)

    def __str__(self):
        return self.name


class TemplateStatusNotification(models.Model):
    status = models.CharField(max_length=255, validators=[validate_name], unique=True)
    html = models.TextField(validators=[validate_name])
    text = models.TextField(validators=[validate_name])
    subject = models.CharField(max_length=255, validators=[validate_name])

    def __str__(self):
        return self.status


class SupportCustomer(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    backend_id = models.CharField(max_length=255, unique=True)


class RequestType(core_models.UuidMixin, core_models.NameMixin, models.Model):
    backend_id = models.IntegerField(unique=True)
    issue_type_name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class IssueStatus(models.Model):
    """ This model is needed in order to understand whether the issue has been solved or not.

        The field of resolution does not give an exact answer since may be the same in both cases.
    """

    class Types(object):
        RESOLVED = 0
        CANCELED = 1

    TYPE_CHOICES = (
        (Types.RESOLVED, 'Resolved'),
        (Types.CANCELED, 'Canceled'),
    )

    name = models.CharField(max_length=255, help_text='Status name in Jira.', unique=True)
    type = FSMIntegerField(default=Types.RESOLVED, choices=TYPE_CHOICES)

    @classmethod
    def check_success_status(cls, status):
        """ Check an issue has been resolved.

            True if an issue resolved.
            False if an issue canceled.
            None in all other cases.
        """
        if not cls.objects.filter(type=cls.Types.RESOLVED).exists() or \
                not cls.objects.filter(type=cls.Types.CANCELED).exists():
            logger.critical('There is no information about statuses of an issue. '
                            'Please, add resolved and cancelled statuses in admin. '
                            'Otherwise, you cannot use processes that need this information.')
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
