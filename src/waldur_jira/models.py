from __future__ import unicode_literals

import re
import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.core.fields import JSONField
from waldur_core.structure import models as structure_models


class JiraService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project, related_name='jira_services', through='JiraServiceProjectLink')

    @classmethod
    def get_url_name(cls):
        return 'jira'


class JiraServiceProjectLink(structure_models.ServiceProjectLink):
    service = models.ForeignKey(JiraService)

    @classmethod
    def get_url_name(cls):
        return 'jira-spl'


class ProjectTemplate(core_models.UiDescribableMixin, structure_models.GeneralServiceProperty):
    @classmethod
    def get_url_name(cls):
        return 'jira-project-templates'

    @classmethod
    def get_backend_fields(cls):
        return super(ProjectTemplate, cls).get_backend_fields() + ('icon_url', 'description')


class Project(structure_models.NewResource, core_models.RuntimeStateMixin):

    class Permissions(structure_models.NewResource.Permissions):
        pass

    service_project_link = models.ForeignKey(
        JiraServiceProjectLink, related_name='projects', on_delete=models.PROTECT)
    template = models.ForeignKey(ProjectTemplate, blank=True, null=True)
    action = models.CharField(max_length=50, blank=True)
    action_details = JSONField(default=dict)

    def get_backend(self):
        return super(Project, self).get_backend(project=self.backend_id)

    def get_access_url(self):
        base_url = self.service_project_link.service.settings.backend_url
        return urlparse.urljoin(base_url, 'projects/' + self.backend_id)

    @classmethod
    def get_url_name(cls):
        return 'jira-projects'

    @property
    def priorities(self):
        return Priority.objects.filter(settings=self.service_project_link.service.settings)


class JiraPropertyIssue(core_models.UuidMixin, core_models.StateMixin, TimeStampedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True)
    backend_id = models.CharField(max_length=255, null=True)

    class Permissions(object):
        customer_path = 'project__service_project_link__project__customer'
        project_path = 'project__service_project_link__project'

    class Meta(object):
        abstract = True


@python_2_unicode_compatible
class IssueType(core_models.UiDescribableMixin, structure_models.ServiceProperty):
    projects = models.ManyToManyField(Project, related_name='issue_types')
    subtask = models.BooleanField(default=False)

    class Meta(structure_models.ServiceProperty.Meta):
        verbose_name = _('Issue type')
        verbose_name_plural = _('Issue types')

    @classmethod
    def get_url_name(cls):
        return 'jira-issue-types'

    def __str__(self):
        return self.name

    @classmethod
    def get_backend_fields(cls):
        return super(IssueType, cls).get_backend_fields() + (
            'icon_url', 'description', 'subtask', 'projects'
        )


@python_2_unicode_compatible
class Priority(core_models.UiDescribableMixin, structure_models.ServiceProperty):

    class Meta(structure_models.ServiceProperty.Meta):
        verbose_name = _('Priority')
        verbose_name_plural = _('Priorities')

    @classmethod
    def get_url_name(cls):
        return 'jira-priorities'

    def __str__(self):
        return self.name

    @classmethod
    def get_backend_fields(cls):
        return super(Priority, cls).get_backend_fields() + ('icon_url', 'description')


@python_2_unicode_compatible
class Issue(structure_models.StructureLoggableMixin,
            JiraPropertyIssue):

    type = models.ForeignKey(IssueType)
    parent = models.ForeignKey('Issue', blank=True, null=True)
    project = models.ForeignKey(Project, related_name='issues')
    summary = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    creator_name = models.CharField(blank=True, max_length=255)
    creator_email = models.CharField(blank=True, max_length=255)
    creator_username = models.CharField(blank=True, max_length=255)
    reporter_name = models.CharField(blank=True, max_length=255)
    reporter_email = models.CharField(blank=True, max_length=255)
    reporter_username = models.CharField(blank=True, max_length=255)
    assignee_name = models.CharField(blank=True, max_length=255)
    assignee_email = models.CharField(blank=True, max_length=255)
    assignee_username = models.CharField(blank=True, max_length=255)
    resolution = models.CharField(blank=True, max_length=255)
    resolution_date = models.CharField(blank=True, null=True, max_length=255)
    priority = models.ForeignKey(Priority)
    status = models.CharField(max_length=255)
    updated = models.DateTimeField(auto_now_add=True)

    resource_content_type = models.ForeignKey(ContentType, blank=True, null=True, related_name='jira_issues')
    resource_object_id = models.PositiveIntegerField(blank=True, null=True)
    resource = GenericForeignKey('resource_content_type', 'resource_object_id')

    resolution_sla = models.IntegerField(blank=True, null=True)

    tracker = FieldTracker()

    class Meta(object):
        unique_together = ('project', 'backend_id')

    def get_backend(self):
        return self.project.get_backend()

    @classmethod
    def get_url_name(cls):
        return 'jira-issues'

    @property
    def key(self):
        return self.backend_id or ''

    @property
    def issue_user(self):
        return self.user  # XXX: avoid logging conflicts

    @property
    def issue_project(self):
        return self.project  # XXX: avoid logging conflicts

    def get_access_url(self):
        base_url = self.project.service_project_link.service.settings.backend_url
        return urlparse.urljoin(base_url, 'browse/' + (self.backend_id or ''))

    def get_log_fields(self):
        return ('uuid', 'issue_user', 'key', 'summary', 'status', 'issue_project')

    def get_description(self):
        template = settings.WALDUR_JIRA['ISSUE_TEMPLATE']['RESOURCE_INFO']
        if template and self.resource:
            return self.description + template.format(resource=self.resource)

        return self.description

    def __str__(self):
        return '{}: {}'.format(self.uuid, self.backend_id or '???')


class JiraSubPropertyIssue(JiraPropertyIssue):

    class Permissions(object):
        customer_path = 'issue__project__service_project_link__project__customer'
        project_path = 'issue__project__service_project_link__project'

    class Meta(object):
        abstract = True


@python_2_unicode_compatible
class Comment(structure_models.StructureLoggableMixin,
              JiraSubPropertyIssue):
    issue = models.ForeignKey(Issue, related_name='comments')
    message = models.TextField(blank=True)

    class Meta(object):
        unique_together = ('issue', 'backend_id')

    def get_backend(self):
        return self.issue.project.get_backend()

    @classmethod
    def get_url_name(cls):
        return 'jira-comments'

    @property
    def comment_user(self):
        return self.user  # XXX: avoid logging conflicts

    def get_log_fields(self):
        return ('uuid', 'comment_user', 'issue')

    def clean_message(self, message):
        template = settings.WALDUR_JIRA['COMMENT_TEMPLATE']
        if not template:
            return self.message

        User = get_user_model()
        template = re.sub(r'([\^~*?:\(\)\[\]|+])', r'\\\1', template)
        pattern = template.format(body='', user=User(full_name=r'(.+?)', username=r'([\w.@+-]+)'))
        match = re.search(pattern, message)

        if match:
            try:
                self.user = User.objects.get(username=match.group(2))
            except User.DoesNotExist:
                pass
            self.message = message[:match.start()]
        else:
            self.message = message

        return self.message

    def prepare_message(self):
        template = settings.WALDUR_JIRA['COMMENT_TEMPLATE']
        if template and self.user:
            return template.format(user=self.user, body=self.message)
        return self.message

    def update_message(self, message):
        self.message = self.clean_message(message)

    def __str__(self):
        return '{}: {}'.format(self.issue.backend_id or '???', self.backend_id or '')


class Attachment(JiraSubPropertyIssue):
    issue = models.ForeignKey(Issue, related_name='attachments')
    file = models.FileField(upload_to='jira_attachments')
    thumbnail = models.FileField(upload_to='jira_attachments_thumbnails', blank=True, null=True)

    class Meta(object):
        unique_together = ('issue', 'backend_id')

    def get_backend(self):
        return self.issue.project.get_backend()

    @classmethod
    def get_url_name(cls):
        return 'jira-attachments'
