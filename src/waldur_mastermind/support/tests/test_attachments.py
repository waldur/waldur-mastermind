from unittest import mock

from ddt import data, ddt, unpack
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status

from waldur_core.configuration.testing import override_config
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.tests.pat_utils import STAFF, SUPPORT, use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models
from waldur_mastermind.support import permissions as support_permissions
from waldur_mastermind.support.backend.zammad import ZammadServiceBackend
from waldur_mastermind.support.tests import base, factories


class AttachmentTest(base.BaseTest):
    """The fixture's issue is raised against a project, by a caller who holds
    no role there, so it is the roles that decide who sees it."""

    def setUp(self):
        super().setUp()
        self.issue = self.fixture.issue
        self.fixture.caller = self.fixture.issue.caller
        self.fixture.unrelated = structure_factories.UserFactory()
        self.attachment = factories.AttachmentFactory(issue=self.issue)
        self.file_path = self.attachment.file.file.name
        self.url = factories.AttachmentFactory.get_url(self.attachment)

    def unscoped_issue(self):
        """A ticket raised against nothing, which its caller alone sees."""
        return factories.IssueFactory(project=None, customer=None)

    def own_attachment(self, user, issue=None):
        author = factories.SupportUserFactory(user=user)
        return factories.AttachmentFactory(issue=issue or self.issue, author=author)


@ddt
class AttachmentGetTest(AttachmentTest):
    @data("staff", "global_support", "owner", "admin", "manager", "member")
    def test_whoever_sees_the_issue_sees_its_attachment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        self.assertEqual(
            self.client.get(factories.IssueFactory.get_url(self.issue)).status_code,
            status.HTTP_200_OK,
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(
            factories.AttachmentFactory.get_list_url(),
            {"issue_uuid": self.issue.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["uuid"] for item in response.data], [self.attachment.uuid.hex]
        )

    @data("caller", "unrelated")
    def test_nobody_else_sees_it(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        self.assertEqual(
            self.client.get(factories.IssueFactory.get_url(self.issue)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        response = self.client.get(factories.AttachmentFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_caller_of_an_unscoped_issue_sees_its_attachment(self):
        issue = self.unscoped_issue()
        attachment = factories.AttachmentFactory(issue=issue)
        self.client.force_authenticate(issue.caller)
        response = self.client.get(factories.AttachmentFactory.get_url(attachment))
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@ddt
class AttachmentCreateTest(AttachmentTest):
    def setUp(self):
        super().setUp()
        self.url = factories.AttachmentFactory.get_list_url()
        self.file = dummy_image()

    def _post(self, issue=None):
        return self.client.post(
            self.url,
            data={
                "issue": factories.IssueFactory.get_url(issue or self.issue),
                "file": self.file,
            },
            format="multipart",
        )

    @data("staff", "global_support", "owner", "admin", "manager")
    def test_whoever_may_comment_may_attach(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Attachment.objects.filter(uuid=response.data["uuid"]).exists()
        )

    def test_member_who_may_not_comment_may_not_attach(self):
        self.client.force_authenticate(self.fixture.member)
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        ("member", status.HTTP_403_FORBIDDEN),
        ("unrelated", status.HTTP_400_BAD_REQUEST),
    )
    @unpack
    def test_refused_attach_leaves_no_support_user(self, user, refused_with):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        self.assertEqual(self._post().status_code, refused_with)
        self.assertFalse(models.SupportUser.objects.filter(user=user).exists())

    @data("caller", "unrelated")
    def test_an_issue_the_user_cannot_see_does_not_exist_for_them(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("issue", response.data)
        self.assertEqual(models.Attachment.objects.filter(issue=self.issue).count(), 1)

    def test_caller_of_an_unscoped_issue_may_attach(self):
        issue = self.unscoped_issue()
        self.client.force_authenticate(issue.caller)
        response = self._post(issue)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_create_attachment_if_issue_is_resolved(self):
        self.client.force_authenticate(self.fixture.staff)
        self.fixture.issue.set_resolved()
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@ddt
class AttachmentDeleteTest(AttachmentTest):
    def delete(self, user, attachment):
        self.client.force_authenticate(user)
        return self.client.delete(factories.AttachmentFactory.get_url(attachment))

    @data("global_support", "owner", "admin", "manager")
    def test_may_delete_own_attachment(self, user):
        user = getattr(self.fixture, user)
        response = self.delete(user, self.own_attachment(user))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("global_support", "owner", "admin", "manager", "member")
    def test_may_not_delete_someone_elses_attachment(self, user):
        response = self.delete(getattr(self.fixture, user), self.attachment)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.Attachment.objects.filter(pk=self.attachment.pk).exists()
        )

    def test_staff_may_delete_any_attachment(self):
        response = self.delete(self.fixture.staff, self.attachment)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_author_who_may_no_longer_comment_may_not_delete(self):
        member = self.fixture.member
        response = self.delete(member, self.own_attachment(member))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("caller", "unrelated")
    def test_nobody_else_finds_it(self, user):
        response = self.delete(getattr(self.fixture, user), self.attachment)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_caller_may_delete_own_attachment_but_not_staffs(self):
        issue = self.unscoped_issue()
        response = self.delete(issue.caller, self.own_attachment(issue.caller, issue))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        staffs = self.own_attachment(self.fixture.staff, issue)
        response = self.delete(issue.caller, staffs)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class AttachmentCanDeleteTest(AttachmentTest):
    """``can_delete`` tells the caller what deleting the attachment would answer."""

    def can_delete(self, user, attachment=None):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.AttachmentFactory.get_url(attachment or self.attachment)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["can_delete"]

    @data("global_support", "owner", "admin", "manager", "member")
    def test_false_on_someone_elses_attachment(self, user):
        self.assertIs(self.can_delete(getattr(self.fixture, user)), False)

    def test_true_for_staff_on_any_attachment(self):
        self.assertIs(self.can_delete(self.fixture.staff), True)

    @data("global_support", "owner", "admin", "manager")
    def test_true_for_the_uploader(self, user):
        user = getattr(self.fixture, user)
        self.assertIs(self.can_delete(user, self.own_attachment(user)), True)

    def test_false_for_an_uploader_who_may_no_longer_comment(self):
        member = self.fixture.member
        self.assertIs(self.can_delete(member, self.own_attachment(member)), False)

    @data("staff", "global_support", "owner", "admin", "manager", "member")
    def test_agrees_with_delete(self, user):
        user = getattr(self.fixture, user)
        for attachment in (
            self.attachment,
            self.own_attachment(user),
            # Synced from a remote desk: no support user, or one with no
            # local user behind it.
            factories.AttachmentFactory(issue=self.issue, author=None),
            factories.AttachmentFactory(
                issue=self.issue, author=factories.SupportUserFactory(user=None)
            ),
        ):
            can_delete = self.can_delete(user, attachment)
            response = self.client.delete(
                factories.AttachmentFactory.get_url(attachment)
            )
            self.assertEqual(
                response.status_code,
                status.HTTP_204_NO_CONTENT if can_delete else status.HTTP_403_FORBIDDEN,
            )

    def test_list_answers_per_attachment_with_one_check_per_uploader(self):
        owner = self.fixture.owner
        author = factories.SupportUserFactory(user=owner)
        own = [
            factories.AttachmentFactory(issue=self.issue, author=author)
            for _ in range(3)
        ]
        self.client.force_authenticate(owner)
        with mock.patch.object(
            support_permissions,
            "can_delete_attachment",
            wraps=support_permissions.can_delete_attachment,
        ) as check:
            response = self.client.get(
                factories.AttachmentFactory.get_list_url(),
                {"issue_uuid": self.issue.uuid.hex},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {item["uuid"]: item["can_delete"] for item in response.data},
            {
                **{attachment.uuid.hex: True for attachment in own},
                self.attachment.uuid.hex: False,
            },
        )
        # The owner's three attachments share one verdict, and the one nobody
        # local uploaded has its own, so the list does not check every row.
        self.assertEqual(check.call_count, 2)


@override_config(PAT_ENABLED=True)
class AttachmentCanDeleteTokenTest(AttachmentTest):
    """With a personal access token, ``can_delete`` still answers what
    deleting would."""

    def answers(self, user, attachment, *scopes):
        use_pat(self.client, user, *scopes)
        url = factories.AttachmentFactory.get_url(attachment)
        can_delete = self.client.get(url).data["can_delete"]
        return can_delete, self.client.delete(url).status_code

    def test_staff_token_with_the_staff_scope_deletes_any(self):
        self.assertEqual(
            self.answers(self.fixture.staff, self.attachment, STAFF),
            (True, status.HTTP_204_NO_CONTENT),
        )

    def test_staff_token_without_it_is_held_to_the_uploader_rule(self):
        staff_owner = structure_factories.UserFactory(is_staff=True)
        self.fixture.customer.add_user(staff_owner, CustomerRole.OWNER)
        self.assertEqual(
            self.answers(staff_owner, self.attachment, SUPPORT),
            (False, status.HTTP_403_FORBIDDEN),
        )
        self.assertEqual(
            self.answers(staff_owner, self.own_attachment(staff_owner), SUPPORT),
            (True, status.HTTP_204_NO_CONTENT),
        )


class ZammadAttachmentDestroyIsAvailableTest(AttachmentTest):
    @mock.patch("waldur_mastermind.support.backend.zammad.ZammadBackend")
    def test_false_not_null_once_a_synced_attachment_is_past_the_cooldown(self, _):
        self.mock_get_active_backend().attachment_destroy_is_available = (
            ZammadServiceBackend().attachment_destroy_is_available
        )
        with freeze_time("2000-11-01T12:00:00"):
            self.attachment.backend_id = "123"
            self.attachment.created = timezone.now()
            self.attachment.save()
        self.client.force_authenticate(self.fixture.staff)

        with freeze_time("2000-11-01T12:01:00"):
            self.assertIs(self.client.get(self.url).data["destroy_is_available"], True)
        with freeze_time("2000-11-01T12:20:00"):
            self.assertIs(self.client.get(self.url).data["destroy_is_available"], False)
