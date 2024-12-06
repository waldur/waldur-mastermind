from unittest import mock

from rest_framework import test

from waldur_core.permissions.fixtures import ProjectRole
from waldur_mastermind.marketplace.tests import (
    factories as marketplace_factories,
)
from waldur_mastermind.marketplace.tests import (
    fixtures as marketplace_fixtures,
)
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support import models as support_models


class TestMembershipChangeIssues(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.plugin_options["enable_issues_for_membership_changes"] = True
        self.offering.type = PLUGIN_NAME
        self.offering.save()
        self.project = self.fixture.project
        self.offering_user = marketplace_factories.OfferingUserFactory(
            offering=self.offering, user=self.fixture.user
        )

        mock_patch = mock.patch("waldur_mastermind.support.backend.get_active_backend")
        self.mock_get_active_backend = mock_patch.start()
        self.mock_get_active_backend().get_issue_details.return_value = {}

    def test_issue_created_when_user_added_to_project(self):
        self.project.add_user(self.fixture.user, ProjectRole.MANAGER)
        self.assertTrue(
            support_models.Issue.objects.filter(
                project=self.project, summary__icontains="added"
            ).exists()
        )
        issue = support_models.Issue.objects.filter(
            project=self.project, summary__icontains="added"
        ).get()
        self.assertTrue(self.offering_user.username in issue.description)

    def test_issue_created_when_user_removed_from_project(self):
        admin = self.fixture.admin
        self.project.remove_user(admin)
        self.assertTrue(
            support_models.Issue.objects.filter(
                project=self.project, summary__icontains="removed"
            ).exists()
        )
