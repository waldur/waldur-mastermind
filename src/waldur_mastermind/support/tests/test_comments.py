from constance.test.pytest import override_config
from ddt import data, ddt
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models
from waldur_mastermind.support.backend.zammad import ZammadServiceBackend
from waldur_mastermind.support.tests import base, factories


@ddt
class CommentCreateTest(base.BaseTest):
    def _get_url(self, issue):
        return factories.IssueFactory.get_url(issue, action="comment")

    @data("staff", "global_support", "owner", "admin", "manager")
    def test_user_with_access_to_issue_can_comment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(
            customer=self.fixture.customer, project=self.fixture.project
        )
        payload = self._get_valid_payload()

        response = self.client.post(self._get_url(issue), data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Comment.objects.filter(
                issue=issue, description=payload["description"]
            )
        )

    @data("owner", "admin", "manager", "user")
    def test_user_can_comment_on_issue_where_he_is_caller_without_project_and_customer(
        self, user
    ):
        current_user = getattr(self.fixture, user)
        self.client.force_authenticate(current_user)
        issue = factories.IssueFactory(caller=current_user, project=None)
        payload = self._get_valid_payload()

        response = self.client.post(self._get_url(issue), data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Comment.objects.filter(
                issue=issue, description=payload["description"]
            )
        )

    @data("admin", "manager", "user")
    def test_user_without_access_to_instance_cannot_comment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(customer=self.fixture.customer)
        payload = self._get_valid_payload()

        response = self.client.post(self._get_url(issue), data=payload)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(
            models.Comment.objects.filter(
                issue=issue, description=payload["description"]
            )
        )

    @override_config(WALDUR_SUPPORT_ENABLED=False)
    def test_user_can_not_comment_issue_if_support_extension_is_disabled(self):
        self.client.force_authenticate(self.fixture.staff)
        issue = factories.IssueFactory(customer=self.fixture.customer)
        payload = self._get_valid_payload()

        response = self.client.post(self._get_url(issue), data=payload)
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)

    def test_create_comment_if_issue_is_resolved(self):
        self.client.force_authenticate(self.fixture.staff)
        issue = factories.IssueFactory(
            customer=self.fixture.customer, project=self.fixture.project
        )
        issue.set_resolved()

        response = self.client.post(
            self._get_url(issue), data=self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def _get_valid_payload(self):
        return {"description": "Comment description"}


@ddt
class CommentUpdateTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.comment = factories.CommentFactory(issue=self.fixture.issue)
        self.url = factories.CommentFactory.get_url(self.comment)

    def test_staff_can_edit_comment(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            models.Comment.objects.filter(description=payload["description"]).exists()
        )

    def test_user_cannot_edit_comment_if_backend_does_not_support_this(self):
        self.mock_get_active_backend().comment_update_is_available.return_value = False
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload()
        response = self.client.patch(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("owner", "admin", "manager")
    def test_nonstaff_user_cannot_edit_comment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.Comment.objects.filter(description=payload["description"]).exists()
        )

    def _get_valid_payload(self):
        return {"description": "New comment description"}


@ddt
class CommentDeleteTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.comment = factories.CommentFactory(issue=self.fixture.issue)
        self.url = factories.CommentFactory.get_url(self.comment)

    def test_staff_can_delete_comment(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Comment.objects.filter(id=self.comment.id).exists())

    def test_user_cannot_delete_comment_if_backend_does_not_support_this(self):
        self.mock_get_active_backend().comment_destroy_is_available.return_value = False
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_delete_new_comment_if_zammad_is_used(self):
        self.mock_get_active_backend().comment_destroy_is_available = (
            lambda x: ZammadServiceBackend.comment_destroy_is_available(None, x)
        )
        self.client.force_authenticate(self.fixture.staff)

        with freeze_time("2000-11-01T12:00:00"):
            self.comment.created = timezone.now()
            self.comment.is_public = False
            self.comment.save()

        with freeze_time("2000-11-01T12:01:00"):
            response = self.client.delete(self.url)
            self.assertEqual(
                response.status_code, status.HTTP_204_NO_CONTENT, response.data
            )

    def test_user_can_not_delete_old_comment_if_zammad_is_used(self):
        self.mock_get_active_backend().comment_destroy_is_available = (
            lambda x: ZammadServiceBackend.comment_destroy_is_available(None, x)
        )
        self.client.force_authenticate(self.fixture.staff)

        with freeze_time("2000-11-01T12:00:00"):
            self.comment.created = timezone.now()
            self.comment.save()

        with freeze_time("2000-11-01T12:20:00"):
            response = self.client.delete(self.url)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("owner", "admin", "manager")
    def test_not_staff_user_cannot_delete_comment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Comment.objects.filter(id=self.comment.id).exists())


@ddt
class CommentRetrieveTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.comment = self.fixture.comment
        self.comment.is_public = True
        self.comment.save()

    @data("owner", "admin", "manager")
    def test_user_can_get_a_public_comment_if_he_is_an_issue_caller_and_has_no_role_access(
        self, user
    ):
        user = getattr(self.fixture, user)
        issue = self.fixture.issue
        issue.caller = user
        issue.customer = None
        issue.project = None
        issue.save()
        # add some noise
        factories.CommentFactory(issue=issue)
        self.client.force_authenticate(user)

        response = self.client.get(
            factories.CommentFactory.get_list_url(), {"issue__uuid": issue.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.comment.uuid.hex)

    @data("owner", "admin", "manager")
    def test_a_public_comment_is_not_duplicated_if_user_is_an_issue_caller_and_has_access(
        self, user
    ):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        issue = self.fixture.issue
        issue.caller = user
        issue.save()
        # add some noise
        factories.CommentFactory(issue=issue)

        response = self.client.get(
            factories.CommentFactory.get_list_url(), {"issue_uuid": issue.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.comment.uuid.hex)

    @data("owner", "admin", "manager")
    def test_user_with_access_to_issue_resource_can_filter_comments_by_resource(
        self, user
    ):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        issue = self.fixture.issue
        issue.resource = self.fixture.resource
        issue.save()
        issue_without_a_resource = factories.IssueFactory(
            customer=self.fixture.customer, project=self.fixture.project
        )
        factories.CommentFactory(issue=issue_without_a_resource, is_public=True)

        payload = {
            "resource": structure_factories.TestNewInstanceFactory.get_url(
                self.fixture.resource
            ),
        }

        response = self.client.get(factories.CommentFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.comment.uuid.hex)

    def test_user_can_get_comment_if_he_is_the_author(self):
        comment = factories.CommentFactory(issue=self.fixture.issue)
        comment.author.user = self.fixture.owner
        comment.author.save()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            factories.CommentFactory.get_list_url(),
            {"issue_uuid": self.fixture.issue.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        self.assertTrue(comment.uuid.hex in [item["uuid"] for item in response.data])
