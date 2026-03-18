from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core.enums import ReviewStates
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure import models
from waldur_core.structure.tests import factories, fixtures


class ProjectEndDateChangeRequestCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.list_url = factories.ProjectEndDateChangeRequestFactory.get_list_url()

    def get_valid_payload(self):
        return {
            "project": factories.ProjectFactory.get_url(self.project),
            "requested_end_date": (timezone.now() + timedelta(days=60))
            .date()
            .isoformat(),
        }

    def test_project_member_without_update_permission_can_create_request(self):
        """Project member (manager/member) without UPDATE_PROJECT can create request."""
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.list_url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["state"], "pending")
        self.assertEqual(
            models.ProjectEndDateChangeRequest.objects.filter(
                project=self.project, created_by=self.fixture.manager
            ).count(),
            1,
        )

    def test_user_with_update_project_permission_cannot_create_request(self):
        """User with UPDATE_PROJECT should edit directly, not create request."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_PROJECT)
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.list_url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("permission", str(response.data).lower())
        self.assertFalse(
            models.ProjectEndDateChangeRequest.objects.filter(
                project=self.project
            ).exists()
        )

    def test_requested_end_date_must_be_in_future(self):
        """Requested end date must be in the future."""
        self.client.force_authenticate(self.fixture.manager)
        payload = self.get_valid_payload()
        payload["requested_end_date"] = (
            (timezone.now() - timedelta(days=1)).date().isoformat()
        )
        response = self.client.post(self.list_url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("requested_end_date", response.data)

    @freeze_time("2024-01-15")
    def test_requested_end_date_today_is_invalid(self):
        """Requested end date cannot be today."""
        self.client.force_authenticate(self.fixture.manager)
        payload = self.get_valid_payload()
        payload["requested_end_date"] = "2024-01-15"
        response = self.client.post(self.list_url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_user_cannot_create_request(self):
        response = self.client.post(self.list_url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_only_create_request_for_accessible_project(self):
        """User can only create request for projects they have access to."""
        other_project = factories.ProjectFactory()
        self.client.force_authenticate(self.fixture.manager)
        payload = {
            "project": factories.ProjectFactory.get_url(other_project),
            "requested_end_date": (timezone.now() + timedelta(days=60))
            .date()
            .isoformat(),
        }
        response = self.client.post(self.list_url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_create_duplicate_pending_request_for_same_project_and_date(
        self,
    ):
        """Only one pending request per project+requested_end_date allowed."""
        requested_date = (timezone.now() + timedelta(days=60)).date()
        factories.ProjectEndDateChangeRequestFactory(
            project=self.project,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
            requested_end_date=requested_date,
        )
        self.client.force_authenticate(self.fixture.manager)
        payload = {
            "project": factories.ProjectFactory.get_url(self.project),
            "requested_end_date": requested_date.isoformat(),
        }
        response = self.client.post(self.list_url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("pending", str(response.data).lower())

    def test_staff_user_cannot_create_request(self):
        """Staff users should use project edit, not create a request."""
        staff_user = factories.UserFactory(is_staff=True)
        self.project.add_user(staff_user, ProjectRole.MEMBER)
        self.client.force_authenticate(staff_user)
        response = self.client.post(self.list_url, self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("staff", str(response.data).lower())
        self.assertFalse(
            models.ProjectEndDateChangeRequest.objects.filter(
                project=self.project
            ).exists()
        )


class ProjectEndDateChangeRequestListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.request = factories.ProjectEndDateChangeRequestFactory(
            project=self.project,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
        )
        self.list_url = factories.ProjectEndDateChangeRequestFactory.get_list_url()

    def test_owner_can_list_requests_for_organization_projects(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_PROJECT)
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 1)
        uuids = [r["uuid"] for r in response.data]
        self.assertIn(str(self.request.uuid), uuids)

    def test_creator_can_see_own_requests(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(
            self.list_url, {"project_uuid": self.project.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 1)

    def test_filter_by_project_uuid(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(
            self.list_url, {"project_uuid": self.project.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for item in response.data:
            self.assertEqual(item["project_uuid"], self.project.uuid.hex)

    def test_filter_by_state(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(self.list_url, {"state": "pending"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_list_endpoint_does_not_have_n_plus_one_queries(self):
        """Regression test: list endpoint uses eager_load to avoid N+1 queries."""
        # Create multiple requests to trigger N+1 if eager_load is missing
        for i in range(5):
            factories.ProjectEndDateChangeRequestFactory(
                project=self.project,
                created_by=self.fixture.manager,
                state=ReviewStates.PENDING,
                requested_end_date=(timezone.now() + timedelta(days=60 + i)).date(),
            )
        self.client.force_authenticate(self.fixture.manager)
        list_url = factories.ProjectEndDateChangeRequestFactory.get_list_url()

        # With eager_load: ~4-10 queries. Without: 5*4+base = 25+ (N+1)
        with CaptureQueriesContext(connection) as context:
            response = self.client.get(
                list_url, {"project_uuid": self.project.uuid.hex}
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 5)
        self.assertLessEqual(
            len(context),
            15,
            f"Too many queries ({len(context)}), possible N+1. Expected <=15 with eager_load.",
        )


class ProjectEndDateChangeRequestRetrieveTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.request = factories.ProjectEndDateChangeRequestFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
        )
        self.detail_url = factories.ProjectEndDateChangeRequestFactory.get_url(
            self.request
        )

    def test_creator_can_retrieve_request(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], str(self.request.uuid))
        self.assertEqual(response.data["state"], "pending")
        self.assertEqual(response.data["project_uuid"], self.fixture.project.uuid.hex)


class ProjectEndDateChangeRequestApproveRejectTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_PROJECT)
        )
        self.request = factories.ProjectEndDateChangeRequestFactory(
            project=self.project,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
            requested_end_date=(timezone.now() + timedelta(days=90)).date(),
        )
        self.approve_url = factories.ProjectEndDateChangeRequestFactory.get_url(
            self.request, action="approve"
        )
        self.reject_url = factories.ProjectEndDateChangeRequestFactory.get_url(
            self.request, action="reject"
        )

    def test_owner_can_approve_request(self):
        """Organization owner can approve request and project end_date is updated."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url, {"comment": "Approved"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.APPROVED)
        self.assertEqual(self.request.reviewed_by, self.fixture.owner)

        self.project.refresh_from_db()
        self.assertEqual(self.project.end_date, self.request.requested_end_date)
        self.assertEqual(self.project.end_date_requested_by, self.fixture.owner)

    def test_owner_can_reject_request(self):
        """Organization owner can reject request."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.reject_url, {"comment": "Rejected"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.REJECTED)
        self.project.refresh_from_db()
        self.assertNotEqual(self.project.end_date, self.request.requested_end_date)

    def test_project_manager_with_update_permission_can_approve(self):
        """Project manager with UPDATE_PROJECT can approve."""
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.addCleanup(
            lambda: ProjectRole.MANAGER.delete_permission(PermissionEnum.UPDATE_PROJECT)
        )
        # Request created by member, manager approves
        self.request.created_by = self.fixture.member
        self.request.save()
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.approve_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_member_without_update_permission_cannot_approve(self):
        """Project member without UPDATE_PROJECT cannot approve."""
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(self.approve_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_approve_non_pending_request(self):
        """Cannot approve request that is not pending."""
        self.request.state = ReviewStates.APPROVED
        self.request.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_cannot_reject_non_pending_request(self):
        """Cannot reject request that is not pending."""
        self.request.state = ReviewStates.REJECTED
        self.request.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.reject_url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @mock.patch(
        "waldur_core.structure.models.Project.save",
        side_effect=Exception("Simulated DB error"),
    )
    def test_approve_atomicity_request_not_approved_if_project_save_fails(
        self, mock_project_save
    ):
        """If project.save() fails, request state must remain PENDING (rollback)."""
        self.client.force_authenticate(self.fixture.owner)
        with self.assertRaises(Exception):
            self.client.post(self.approve_url, {"comment": "Approved"})

        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.PENDING)
        self.project.refresh_from_db()
        self.assertNotEqual(self.project.end_date, self.request.requested_end_date)


@override_settings(task_always_eager=True)
class ProjectEndDateChangeRequestNotificationTest(test.APITransactionTestCase):
    @mock.patch(
        "waldur_core.structure.handlers.tasks.send_project_end_date_change_request_notification"
    )
    def test_notification_sent_when_request_created(self, mock_task):
        """Notification task is scheduled when request is created."""
        mock_task.delay = mock.Mock()
        self.fixture = fixtures.ProjectFixture()
        self.list_url = factories.ProjectEndDateChangeRequestFactory.get_list_url()
        payload = {
            "project": factories.ProjectFactory.get_url(self.fixture.project),
            "requested_end_date": (timezone.now() + timedelta(days=60))
            .date()
            .isoformat(),
        }
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.list_url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_task.delay.assert_called_once()
        call_args = mock_task.delay.call_args[0]
        self.assertEqual(len(call_args), 1)
        # UUID hex string
        self.assertEqual(len(call_args[0]), 32)

    @mock.patch(
        "waldur_core.structure.handlers.tasks.send_project_end_date_change_request_approved_notification"
    )
    def test_notification_sent_to_requester_when_request_approved(self, mock_task):
        """Approved notification task is scheduled when request is approved."""
        mock_task.delay = mock.Mock()
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_PROJECT)
        )
        request = factories.ProjectEndDateChangeRequestFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
            requested_end_date=(timezone.now() + timedelta(days=90)).date(),
        )
        approve_url = factories.ProjectEndDateChangeRequestFactory.get_url(
            request, action="approve"
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(approve_url, {"comment": "Approved"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.delay.assert_called_once_with(request.uuid.hex)

    @mock.patch(
        "waldur_core.structure.handlers.tasks.send_project_end_date_change_request_rejected_notification"
    )
    def test_notification_sent_to_requester_when_request_rejected(self, mock_task):
        """Rejected notification task is scheduled when request is rejected."""
        mock_task.delay = mock.Mock()
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_PROJECT)
        )
        request = factories.ProjectEndDateChangeRequestFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
            requested_end_date=(timezone.now() + timedelta(days=90)).date(),
        )
        reject_url = factories.ProjectEndDateChangeRequestFactory.get_url(
            request, action="reject"
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(reject_url, {"comment": "Rejected"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.delay.assert_called_once_with(request.uuid.hex)


class ProjectEndDateChangeRequestEventTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.request = factories.ProjectEndDateChangeRequestFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
        )

    def test_event_created_when_request_created(self):
        """Event is logged when request is created via API."""
        initial_count = Event.objects.filter(
            event_type="project_end_date_change_request_created"
        ).count()
        self.list_url = factories.ProjectEndDateChangeRequestFactory.get_list_url()
        payload = {
            "project": factories.ProjectFactory.get_url(self.fixture.project),
            "requested_end_date": (timezone.now() + timedelta(days=60))
            .date()
            .isoformat(),
        }
        self.client.force_authenticate(self.fixture.manager)
        self.client.post(self.list_url, payload)
        self.assertEqual(
            Event.objects.filter(
                event_type="project_end_date_change_request_created"
            ).count(),
            initial_count + 1,
        )
