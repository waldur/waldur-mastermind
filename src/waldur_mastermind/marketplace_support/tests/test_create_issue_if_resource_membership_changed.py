from unittest import mock

from django.contrib.contenttypes.models import ContentType
from rest_framework import test

from waldur_core.permissions.models import Role
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    SUPPORT_OFFERING,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import (
    factories as marketplace_factories,
)
from waldur_mastermind.marketplace.tests import (
    fixtures as marketplace_fixtures,
)
from waldur_mastermind.support import models as support_models


class TestResourceMembershipChangeIssues(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.plugin_options["enable_issues_for_membership_changes"] = True
        self.offering.type = SUPPORT_OFFERING
        self.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.offering_user = marketplace_factories.OfferingUserFactory(
            offering=self.offering, user=self.fixture.user
        )

        self.resource_role = Role.objects.create(
            name="resource-admin",
            content_type=ContentType.objects.get_for_model(marketplace_models.Resource),
            is_system_role=False,
        )
        self.resource_project = marketplace_models.ResourceProject.objects.create(
            resource=self.resource, name="Sub-project A"
        )
        self.resource_project_role = Role.objects.create(
            name="project-admin",
            content_type=ContentType.objects.get_for_model(
                marketplace_models.ResourceProject
            ),
            is_system_role=False,
        )

        mock_patch = mock.patch("waldur_mastermind.support.backend.get_active_backend")
        self.mock_get_active_backend = mock_patch.start()
        self.addCleanup(mock_patch.stop)
        self.mock_get_active_backend().get_issue_details.return_value = {}

    def test_issue_created_when_user_added_to_resource(self):
        self.resource.add_user(self.fixture.user, self.resource_role)
        issues = support_models.Issue.objects.filter(
            project=self.resource.project, summary__icontains="added"
        )
        self.assertTrue(issues.exists())
        issue = issues.get()
        self.assertIn(self.resource.name, issue.summary)
        self.assertIn(self.resource_role.name, issue.summary)
        self.assertIn(self.offering_user.username, issue.description)

    def test_issue_created_when_user_removed_from_resource(self):
        permission = self.resource.add_user(self.fixture.user, self.resource_role)
        support_models.Issue.objects.all().delete()
        permission.revoke()
        issues = support_models.Issue.objects.filter(
            project=self.resource.project, summary__icontains="removed"
        )
        self.assertTrue(issues.exists())
        issue = issues.get()
        self.assertIn(self.resource.name, issue.summary)
        self.assertIn(self.resource_role.name, issue.summary)

    def test_issue_created_when_user_added_to_resource_project(self):
        self.resource_project.add_user(self.fixture.user, self.resource_project_role)
        issues = support_models.Issue.objects.filter(
            project=self.resource.project, summary__icontains="added"
        )
        self.assertTrue(issues.exists())
        issue = issues.get()
        self.assertIn(self.resource.name, issue.summary)
        self.assertIn(self.resource.project.name, issue.summary)
        self.assertIn(self.resource_project_role.name, issue.summary)

    def test_issue_created_when_user_removed_from_resource_project(self):
        permission = self.resource_project.add_user(
            self.fixture.user, self.resource_project_role
        )
        support_models.Issue.objects.all().delete()
        permission.revoke()
        issues = support_models.Issue.objects.filter(
            project=self.resource.project, summary__icontains="removed"
        )
        self.assertTrue(issues.exists())
        issue = issues.get()
        self.assertIn(self.resource.name, issue.summary)
        self.assertIn(self.resource.project.name, issue.summary)

    def test_no_issue_when_offering_is_not_support_type(self):
        self.offering.type = BASIC_OFFERING
        self.offering.save()
        self.resource.add_user(self.fixture.user, self.resource_role)
        self.assertFalse(
            support_models.Issue.objects.filter(project=self.resource.project).exists()
        )

    def test_no_issue_when_flag_is_disabled(self):
        self.offering.plugin_options["enable_issues_for_membership_changes"] = False
        self.offering.save()
        self.resource.add_user(self.fixture.user, self.resource_role)
        self.assertFalse(
            support_models.Issue.objects.filter(project=self.resource.project).exists()
        )

    def test_no_issue_when_resource_is_terminated(self):
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save()
        self.resource.add_user(self.fixture.user, self.resource_role)
        self.assertFalse(
            support_models.Issue.objects.filter(project=self.resource.project).exists()
        )

    def test_no_issue_when_resource_project_parent_is_terminated(self):
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save()
        self.resource_project.add_user(self.fixture.user, self.resource_project_role)
        self.assertFalse(
            support_models.Issue.objects.filter(project=self.resource.project).exists()
        )
