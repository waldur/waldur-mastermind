from django.utils.functional import cached_property

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

from ..apps import JiraConfig
from . import factories


class JiraFixture(structure_fixtures.ProjectFixture):
    @cached_property
    def service_settings(self):
        return structure_factories.ServiceSettingsFactory(
            type=JiraConfig.service_name,
            backend_url="http://jira/",
            customer=self.customer,
        )

    @cached_property
    def jira_project(self):
        return factories.ProjectFactory(
            service_settings=self.service_settings, project=self.project
        )

    @cached_property
    def jira_project_url(self):
        return factories.ProjectFactory.get_url(self.jira_project)

    @cached_property
    def jira_project_template(self):
        return factories.ProjectTemplateFactory()

    @cached_property
    def jira_project_template_url(self):
        return factories.ProjectTemplateFactory.get_url(self.jira_project_template)

    @cached_property
    def issue_type(self):
        return factories.IssueTypeFactory(settings=self.service_settings)

    @cached_property
    def issue_type_url(self):
        return factories.IssueTypeFactory.get_url(self.issue_type)

    @cached_property
    def priority(self):
        return factories.PriorityFactory(settings=self.service_settings)

    @cached_property
    def priority_url(self):
        return factories.PriorityFactory.get_url(self.priority)
