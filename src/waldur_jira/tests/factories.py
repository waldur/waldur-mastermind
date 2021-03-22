import factory
from django.urls import reverse

from waldur_core.structure.tests import factories as structure_factories

from .. import models
from ..apps import JiraConfig


class JiraServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    type = JiraConfig.service_name
    backend_url = 'http://jira/'


class ProjectTemplateFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.ProjectTemplate

    name = factory.Sequence(lambda n: 'template-%s' % n)
    backend_id = factory.Sequence(lambda n: 'template-%s' % n)

    @classmethod
    def get_url(cls, project=None, action=None):
        if project is None:
            project = ProjectTemplateFactory()
        url = 'http://testserver' + reverse(
            'jira-project-templates-detail', kwargs={'uuid': project.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('jira-project-templates-list')


class ProjectFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Project

    backend_id = factory.Sequence(lambda n: 'PRJ-%s' % n)
    name = factory.Sequence(lambda n: 'JIRA project %s' % n)
    service_settings = factory.SubFactory(JiraServiceSettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    template = factory.SubFactory(ProjectTemplateFactory)
    state = models.Project.States.OK

    @classmethod
    def get_url(cls, project=None, action=None):
        if project is None:
            project = ProjectFactory()
        url = 'http://testserver' + reverse(
            'jira-projects-detail', kwargs={'uuid': project.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('jira-projects-list')
        return url if action is None else url + action + '/'


class IssueTypeFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.IssueType

    settings = factory.SubFactory(JiraServiceSettingsFactory)
    name = factory.Sequence(lambda n: 'issue-type-%s' % n)
    backend_id = factory.Sequence(lambda n: 'issue-type-%s' % n)
    icon_url = factory.Sequence(lambda n: 'http://icon.com/icon_url-%s' % n)

    @classmethod
    def get_url(cls, issue=None, action=None):
        if issue is None:
            issue = IssueTypeFactory()
        url = 'http://testserver' + reverse(
            'jira-issue-types-detail', kwargs={'uuid': issue.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('jira-issue-types-list')


class PriorityFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Priority

    settings = factory.SubFactory(JiraServiceSettingsFactory)
    name = factory.Sequence(lambda n: 'priority-%s' % n)
    backend_id = factory.Sequence(lambda n: 'priority-%s' % n)
    icon_url = factory.Sequence(lambda n: 'http://icon.com/icon_url-%s' % n)

    @classmethod
    def get_url(cls, issue=None, action=None):
        if issue is None:
            issue = PriorityFactory()
        url = 'http://testserver' + reverse(
            'jira-priorities-detail', kwargs={'uuid': issue.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('jira-priorities-list')


class IssueFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Issue

    type = factory.SubFactory(IssueTypeFactory)
    priority = factory.SubFactory(PriorityFactory)
    backend_id = factory.Sequence(lambda n: 'TST-%s' % n)
    status = factory.Sequence(lambda n: 'STATUS-%s' % n)
    assignee_name = factory.Sequence(lambda n: 'ASSIGNEE-%s' % n)
    reporter_name = factory.Sequence(lambda n: 'REPORTER-%s' % n)
    creator_name = factory.Sequence(lambda n: 'CREATOR-%s' % n)
    resolution_date = factory.Sequence(lambda n: 'RESOLUTION_DATE-%s' % n)
    project = factory.SubFactory(ProjectFactory)

    @classmethod
    def get_url(cls, issue=None, action=None):
        if issue is None:
            issue = IssueFactory()
        url = 'http://testserver' + reverse(
            'jira-issues-detail', kwargs={'uuid': issue.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('jira-issues-list')


class CommentFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Comment

    issue = factory.SubFactory(IssueFactory)
    backend_id = factory.Sequence(lambda n: 'TST-%s' % n)

    @classmethod
    def get_url(cls, comment=None, action=None):
        if comment is None:
            comment = CommentFactory()
        url = 'http://testserver' + reverse(
            'jira-comments-detail', kwargs={'uuid': comment.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('jira-comments-list')
