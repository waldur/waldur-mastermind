"""Helpdesk writes, and attachment downloads, reserved to staff or support. A staff or support user's
personal access token without the matching scope acts as the user without that
role (#980).
"""

from django.urls import reverse
from rest_framework import status

from waldur_core.configuration.testing import override_config
from waldur_core.media import models as media_models
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.tests.pat_utils import STAFF, SUPPORT, use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests.factories import OfferingFactory
from waldur_mastermind.support import models
from waldur_mastermind.support.tests import base, factories
from waldur_mastermind.support.tests.test_issues import IssueCreateBaseTest
from waldur_mastermind.support.tests.test_provider_api import (
    ProviderHelpdeskBaseTest,
    _provider_ticket_url,
)


@override_config(PAT_ENABLED=True)
class ProviderTicketTokenScopeTest(ProviderHelpdeskBaseTest):
    """Any provider's routed ticket is reached through staff or support."""

    def setUp(self):
        super().setUp()
        self.child_issue = factories.IssueFactory(
            parent_issue=factories.IssueFactory(backend_id="WLD-900"),
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-901",
        )
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)

    def comment(self):
        return self.client.post(
            _provider_ticket_url(self.child_issue, action="comment"),
            {"description": "note"},
            format="json",
        )

    def resolve(self):
        return self.client.post(_provider_ticket_url(self.child_issue, "resolve"))

    def comments(self):
        return models.Comment.objects.filter(issue=self.child_issue).count()

    def test_staff_session_can_comment(self):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.comment().status_code, status.HTTP_201_CREATED)

    def test_staff_token_with_staff_scope_can_comment(self):
        use_pat(self.client, self.staff, STAFF)
        self.assertEqual(self.comment().status_code, status.HTTP_201_CREATED)

    def test_staff_token_without_staff_scope_cannot_comment(self):
        use_pat(self.client, self.staff)
        self.assertEqual(self.comment().status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.comments(), 0)

    def test_support_token_with_support_scope_can_comment(self):
        # As a support session can: support acts on helpdesk tickets.
        use_pat(self.client, self.support, SUPPORT)
        self.assertEqual(self.comment().status_code, status.HTTP_201_CREATED)

    def test_support_token_without_support_scope_cannot_comment(self):
        use_pat(self.client, self.support)
        self.assertEqual(self.comment().status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.comments(), 0)

    def test_staff_token_without_staff_scope_cannot_resolve(self):
        use_pat(self.client, self.staff)
        self.assertEqual(self.resolve().status_code, status.HTTP_404_NOT_FOUND)

    def test_other_user_cannot_comment(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assertEqual(self.comment().status_code, status.HTTP_404_NOT_FOUND)

    def test_provider_owner_token_can_still_comment(self):
        use_pat(self.client, self.owner)
        self.assertEqual(self.comment().status_code, status.HTTP_201_CREATED)


@override_config(PAT_ENABLED=True)
class CommentChangeTokenScopeTest(base.BaseTest):
    """Staff may change any comment, including whether it is public."""

    def setUp(self):
        super().setUp()
        self.comment = factories.CommentFactory(
            issue=self.fixture.issue, is_public=True
        )
        self.url = factories.CommentFactory.get_url(self.comment)

    def hide(self):
        return self.client.patch(
            self.url, {"description": "Changed", "is_public": False}
        )

    def assert_changed(self, changed):
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.description == "Changed", changed)
        self.assertEqual(self.comment.is_public, not changed)

    def test_staff_session_can_change(self):
        self.client.force_authenticate(self.fixture.staff)
        self.assertEqual(self.hide().status_code, status.HTTP_200_OK)
        self.assert_changed(True)

    def test_staff_token_with_staff_scope_can_change(self):
        use_pat(self.client, self.fixture.staff, STAFF)
        self.assertEqual(self.hide().status_code, status.HTTP_200_OK)
        self.assert_changed(True)

    def test_staff_token_without_staff_scope_cannot_change(self):
        use_pat(self.client, self.fixture.staff)
        self.assertEqual(self.hide().status_code, status.HTTP_403_FORBIDDEN)
        self.assert_changed(False)

    def test_staff_token_without_staff_scope_cannot_delete(self):
        use_pat(self.client, self.fixture.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Comment.objects.filter(pk=self.comment.pk).exists())

    def test_owner_cannot_change(self):
        self.client.force_authenticate(self.fixture.owner)
        self.assertEqual(self.hide().status_code, status.HTTP_403_FORBIDDEN)
        self.assert_changed(False)


@override_config(PAT_ENABLED=True)
class IssueOnBehalfTokenScopeTest(IssueCreateBaseTest):
    """Reporting an issue for another caller is reserved to staff or support."""

    def setUp(self):
        super().setUp()
        factories.SupportCustomerFactory(user=self.caller)

    def report(self, user, *scopes):
        factories.SupportUserFactory(user=user)
        use_pat(self.client, user, *scopes)
        payload = self._get_valid_payload()
        response = self.client.post(self.url, data=payload)
        return response, models.Issue.objects.filter(summary=payload["summary"])

    def test_support_session_reports_for_another_caller(self):
        factories.SupportUserFactory(user=self.fixture.global_support)
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.post(self.url, data=self._get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_support_token_with_support_scope_reports_for_another_caller(self):
        response, issues = self.report(self.fixture.global_support, SUPPORT)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(issues.get().caller, self.caller)

    def test_staff_token_without_staff_scope_cannot_report_for_another_caller(self):
        response, issues = self.report(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(issues.exists())

    def test_support_token_without_support_scope_cannot_report_for_another_caller(
        self,
    ):
        response, issues = self.report(self.fixture.global_support)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(issues.exists())

    def test_user_cannot_report_for_another_caller(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(self.url, data=self._get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_config(PAT_ENABLED=True)
class IssueOfferingTokenScopeTest(IssueCreateBaseTest):
    """Staff and support may name any offering, as the token's scope allows."""

    def setUp(self):
        super().setUp()
        # Another organization's private offering, outside every catalog.
        self.offering = OfferingFactory(
            customer=structure_factories.CustomerFactory(), shared=False
        )

    def report(self, user, *scopes):
        use_pat(self.client, user, *scopes)
        payload = self._get_valid_payload(
            offering=self.offering.uuid.hex, is_reported_manually=True
        )
        return self.client.post(self.url, data=payload)

    def issues(self):
        return models.Issue.objects.filter(offering=self.offering)

    def test_staff_token_with_staff_scope_names_any_offering(self):
        response = self.report(self.fixture.staff, STAFF)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(self.issues().exists())

    def test_support_token_with_support_scope_names_any_offering(self):
        response = self.report(self.fixture.global_support, SUPPORT)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_staff_token_without_staff_scope_cannot_name_a_hidden_offering(self):
        response = self.report(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering", response.data)
        self.assertFalse(self.issues().exists())

    def test_support_token_without_support_scope_cannot_name_a_hidden_offering(
        self,
    ):
        response = self.report(self.fixture.global_support)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering", response.data)
        self.assertFalse(self.issues().exists())


@override_config(PAT_ENABLED=True)
class AttachmentTokenScopeTest(base.BaseTest):
    """Attachments of any issue are reached through staff."""

    def setUp(self):
        super().setUp()
        self.attachment = factories.AttachmentFactory(issue=self.fixture.issue)
        self.url = factories.AttachmentFactory.get_url(self.attachment)
        media_file = media_models.File.objects.get(name=self.attachment.file.name)
        self.media_url = reverse("media", kwargs={"uuid": media_file.uuid})

    def add(self):
        return self.client.post(
            factories.AttachmentFactory.get_list_url(),
            {
                "issue": factories.IssueFactory.get_url(self.fixture.issue),
                "file": dummy_image(),
            },
            format="multipart",
        )

    def attachments(self):
        return models.Attachment.objects.filter(issue=self.fixture.issue).count()

    def test_staff_session_can_add_delete_and_download(self):
        self.client.force_authenticate(self.fixture.staff)
        self.assertEqual(self.add().status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.client.get(self.media_url).status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self.client.delete(self.url).status_code, status.HTTP_204_NO_CONTENT
        )

    def test_staff_token_with_staff_scope_can_add_delete_and_download(self):
        use_pat(self.client, self.fixture.staff, STAFF)
        self.assertEqual(self.add().status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.client.get(self.media_url).status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self.client.delete(self.url).status_code, status.HTTP_204_NO_CONTENT
        )

    def test_staff_token_without_staff_scope_cannot_add_delete_or_download(self):
        use_pat(self.client, self.fixture.staff)
        self.assertEqual(self.add().status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.client.get(self.media_url).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(
            self.client.delete(self.url).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(self.attachments(), 1)

    def test_owner_token_still_adds_and_downloads(self):
        use_pat(self.client, self.fixture.owner)
        self.assertEqual(self.add().status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.client.get(self.media_url).status_code, status.HTTP_200_OK
        )

    def test_project_admin_cannot_add_or_delete(self):
        self.client.force_authenticate(self.fixture.admin)
        self.assertEqual(self.add().status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.client.delete(self.url).status_code, status.HTTP_404_NOT_FOUND
        )
