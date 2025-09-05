from rest_framework import status

from waldur_mastermind.support import models
from waldur_mastermind.support.tests import base


class IssueStatusTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.issue_status = models.IssueStatus.objects.create(
            name="Resolved", type=models.IssueStatus.Types.RESOLVED
        )
        self.url = f"/api/support-issue-statuses/{self.issue_status.uuid}/"
        self.list_url = "/api/support-issue-statuses/"

    def test_staff_can_view_issue_status(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Resolved")
        self.assertEqual(response.data["type"], models.IssueStatus.Types.RESOLVED)
        self.assertEqual(response.data["type_display"], "Resolved")

    def test_support_cannot_view_issue_status(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_user_cannot_view_issue_status(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_list_issue_statuses(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 1)
        # Check our test object is in the response
        names = [item["name"] for item in response.data]
        self.assertIn("Resolved", names)

    def test_support_cannot_list_issue_statuses(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_user_cannot_list_issue_statuses(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_create_issue_status(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {"name": "Canceled", "type": models.IssueStatus.Types.CANCELED}
        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.IssueStatus.objects.filter(name="Canceled").exists())

    def test_support_cannot_create_issue_status(self):
        self.client.force_authenticate(self.fixture.global_support)
        data = {"name": "Canceled", "type": models.IssueStatus.Types.CANCELED}
        response = self.client.post(self.list_url, data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_update_issue_status(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {"name": "Updated Status", "type": models.IssueStatus.Types.CANCELED}
        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.issue_status.refresh_from_db()
        self.assertEqual(self.issue_status.name, "Updated Status")

    def test_support_cannot_update_issue_status(self):
        self.client.force_authenticate(self.fixture.global_support)
        data = {"name": "Updated Status", "type": models.IssueStatus.Types.CANCELED}
        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_delete_issue_status(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.IssueStatus.objects.filter(uuid=self.issue_status.uuid).exists()
        )

    def test_support_cannot_delete_issue_status(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
