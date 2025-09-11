from datetime import timedelta

from ddt import data, ddt
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.structure import models, tasks
from waldur_core.structure.tests import factories, fixtures


@ddt
class ProjectPermissionReviewCloseActionTest(APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.review = factories.ProjectPermissionReviewFactory(project=self.project)
        self.assertTrue(self.review.is_pending)
        self.close_url = factories.ProjectPermissionReviewFactory.get_url(
            self.review, action="close"
        )

    @data("staff", "manager")
    def test_user_can_close_project_permission_review(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.close_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.review.refresh_from_db()
        self.assertFalse(self.review.is_pending)

    @data("owner", "user", "admin", "member")
    def test_user_cannot_get_project_permission_review(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.close_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        self.review.refresh_from_db()
        self.assertTrue(self.review.is_pending)


class CreateProjectPermissionReviewsTest(APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project

    def test_scheduled_task_does_not_create_review_for_project_that_already_has_pending_review(
        self,
    ):
        factories.ProjectPermissionReviewFactory(project=self.project, is_pending=True)
        tasks.create_project_permission_reviews()
        self.assertEqual(
            models.ProjectPermissionReview.objects.filter(project=self.project).count(),
            1,
        )

    def test_scheduled_task_does_not_create_review_for_project_that_does_not_have_enough_users(
        self,
    ):
        self.assertEqual(self.project.get_users().count(), 0)
        tasks.create_project_permission_reviews()
        self.assertFalse(
            models.ProjectPermissionReview.objects.filter(project=self.project).exists()
        )

    def test_scheduled_task_creates_review(self):
        _ = self.fixture.manager
        _ = self.fixture.member
        self.assertGreaterEqual(self.project.get_users().count(), 2)
        tasks.create_project_permission_reviews()
        self.assertTrue(
            models.ProjectPermissionReview.objects.filter(
                project=self.project, is_pending=True
            ).exists()
        )

    def test_scheduled_task_does_not_create_review_for_project_with_recent_closed_review(
        self,
    ):
        _ = self.fixture.manager
        _ = self.fixture.member
        self.assertGreaterEqual(self.project.get_users().count(), 2)
        factories.ProjectPermissionReviewFactory(
            project=self.project,
            is_pending=False,
            closed=timezone.now() - timedelta(days=30),
        )
        tasks.create_project_permission_reviews()
        self.assertEqual(
            models.ProjectPermissionReview.objects.filter(project=self.project).count(),
            1,
        )

    def test_scheduled_task_creates_review_if_last_closed_review_is_older_than_90_days(
        self,
    ):
        _ = self.fixture.manager
        _ = self.fixture.member
        self.assertGreaterEqual(self.project.get_users().count(), 2)
        factories.ProjectPermissionReviewFactory(
            project=self.project,
            is_pending=False,
            closed=timezone.now() - timedelta(days=120),
        )
        tasks.create_project_permission_reviews()
        self.assertTrue(
            models.ProjectPermissionReview.objects.filter(
                project=self.project, is_pending=True
            ).exists()
        )
