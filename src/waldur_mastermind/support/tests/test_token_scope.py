"""Helpdesk writes, and attachment downloads, reserved to staff or support. A staff or support user's
personal access token without the matching scope acts as the user without that
role (#980).
"""

from django.urls import reverse
from rest_framework import status

from waldur_core.configuration.testing import override_config
from waldur_core.media import models as media_models
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.fixtures import CustomerRole
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
class ProviderHelpdeskListTokenScopeTest(ProviderHelpdeskBaseTest):
    """Every provider's helpdesk is listed to staff and support; anyone else,
    a staff or support token without its scope included, sees the helpdesks of
    the providers whose organization they own (#977)."""

    def setUp(self):
        super().setUp()
        self.other_helpdesk = factories.ProviderHelpdeskFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)

    def listed(self):
        response = self.client.get(factories.ProviderHelpdeskFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def retrieve_other(self):
        return self.client.get(
            factories.ProviderHelpdeskFactory.get_url(self.other_helpdesk)
        )

    def all_helpdesks(self):
        return {self.helpdesk.uuid.hex, self.other_helpdesk.uuid.hex}

    def assert_lists_all(self):
        self.assertEqual(self.listed(), self.all_helpdesks())
        self.assertEqual(self.retrieve_other().status_code, status.HTTP_200_OK)

    def assert_lists_only_owned(self, owned):
        self.assertEqual(self.listed(), owned)
        self.assertEqual(self.retrieve_other().status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_session_lists_all(self):
        self.client.force_authenticate(self.staff)
        self.assert_lists_all()

    def test_support_session_lists_all(self):
        self.client.force_authenticate(self.support)
        self.assert_lists_all()

    def test_staff_token_with_staff_scope_lists_all(self):
        use_pat(self.client, self.staff, STAFF)
        self.assert_lists_all()

    def test_support_token_with_support_scope_lists_all(self):
        use_pat(self.client, self.support, SUPPORT)
        self.assert_lists_all()

    def test_staff_token_without_staff_scope_lists_none(self):
        use_pat(self.client, self.staff)
        self.assert_lists_only_owned(set())

    def test_support_token_without_support_scope_lists_none(self):
        use_pat(self.client, self.support)
        self.assert_lists_only_owned(set())

    def test_staff_owner_token_without_staff_scope_lists_own_helpdesk(self):
        self.customer.add_user(self.staff, CustomerRole.OWNER)
        use_pat(self.client, self.staff)
        self.assert_lists_only_owned({self.helpdesk.uuid.hex})

    def test_support_owner_token_without_support_scope_lists_own_helpdesk(self):
        self.customer.add_user(self.support, CustomerRole.OWNER)
        use_pat(self.client, self.support)
        self.assert_lists_only_owned({self.helpdesk.uuid.hex})

    def test_owner_session_and_token_list_own_helpdesk(self):
        self.client.force_authenticate(self.owner)
        self.assert_lists_only_owned({self.helpdesk.uuid.hex})
        use_pat(self.client, self.owner)
        self.assert_lists_only_owned({self.helpdesk.uuid.hex})


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
        # Without its scope the token sees the issue as its user would without
        # staff, which is not at all, so the issue does not exist for it.
        self.assertEqual(self.add().status_code, status.HTTP_400_BAD_REQUEST)
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

    def test_support_token_with_support_scope_can_download(self):
        use_pat(self.client, self.fixture.global_support, SUPPORT)
        self.assertEqual(
            self.client.get(self.media_url).status_code, status.HTTP_200_OK
        )

    def test_support_token_without_support_scope_cannot_download(self):
        use_pat(self.client, self.fixture.global_support)
        self.assertEqual(
            self.client.get(self.media_url).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_404_NOT_FOUND
        )

    def test_can_delete_follows_the_tokens_staff_scope(self):
        # The staff user also owns the issue's organization, so a token without
        # the staff scope still sees the attachment, as an owner who did not
        # upload it.
        staff = self.fixture.staff
        self.fixture.customer.add_user(staff, CustomerRole.OWNER)
        use_pat(self.client, staff)
        self.assertIs(self.client.get(self.url).data["can_delete"], False)
        use_pat(self.client, staff, STAFF)
        self.assertIs(self.client.get(self.url).data["can_delete"], True)

    def test_project_admin_adds_and_deletes_only_their_own(self):
        # A project admin sees the issue and may comment on it, so they may
        # attach to it; deleting someone else's attachment is for staff.
        self.client.force_authenticate(self.fixture.admin)
        response = self.add()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.client.delete(self.url).status_code, status.HTTP_403_FORBIDDEN
        )
        own = factories.AttachmentFactory.get_url(
            models.Attachment.objects.get(uuid=response.data["uuid"])
        )
        self.assertEqual(
            self.client.delete(own).status_code, status.HTTP_204_NO_CONTENT
        )
