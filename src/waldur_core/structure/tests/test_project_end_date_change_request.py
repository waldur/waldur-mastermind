from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.enums import ReviewStates
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.tests.pat_utils import use_pat
from waldur_core.structure import models
from waldur_core.structure.tests import factories, fixtures


def grant_end_date_requests():
    """The project roles ask for a later end date (PROJECT.REQUEST_END_DATE_CHANGE),
    as permissions.yaml grants them; tests do not load it."""
    for role in (ProjectRole.ADMIN, ProjectRole.MANAGER, ProjectRole.MEMBER):
        role.add_permission(PermissionEnum.REQUEST_PROJECT_END_DATE_CHANGE)


class ProjectEndDateChangeRequestCreateTest(test.APITestCase):
    def setUp(self):
        grant_end_date_requests()
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

    def test_user_who_sets_the_end_date_cannot_create_request(self):
        """A holder of PROJECT.SET_END_DATE sets it directly, not by request."""
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_PROJECT_END_DATE)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.SET_PROJECT_END_DATE
            )
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
        grant_end_date_requests()
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.request = factories.ProjectEndDateChangeRequestFactory(
            project=self.project,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
        )
        self.list_url = factories.ProjectEndDateChangeRequestFactory.get_list_url()

    def test_owner_can_list_requests_for_organization_projects(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_PROJECT_END_DATE)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.SET_PROJECT_END_DATE
            )
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
        grant_end_date_requests()
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
        grant_end_date_requests()
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_PROJECT_END_DATE)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.SET_PROJECT_END_DATE
            )
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

    def test_project_manager_with_set_end_date_permission_can_approve(self):
        """Approving takes the permission that sets the date directly."""
        ProjectRole.MANAGER.add_permission(PermissionEnum.SET_PROJECT_END_DATE)
        self.addCleanup(
            lambda: ProjectRole.MANAGER.delete_permission(
                PermissionEnum.SET_PROJECT_END_DATE
            )
        )
        # Request created by member, manager approves
        self.request.created_by = self.fixture.member
        self.request.save()
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.approve_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_updating_the_project_does_not_approve(self):
        """PROJECT.UPDATE used to approve, writing a date its holder could not
        set directly."""
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.addCleanup(
            lambda: ProjectRole.MANAGER.delete_permission(PermissionEnum.UPDATE_PROJECT)
        )
        self.request.created_by = self.fixture.member
        self.request.save()
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.approve_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_member_without_update_permission_cannot_approve(self):
        """A project member does not approve."""
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


@override_config(PAT_ENABLED=True)
class ProjectEndDateChangeRequestTokenTest(test.APITestCase):
    """A personal access token acts within its scopes and bindings.

    Approving or rejecting needs PROJECT.SET_END_DATE among the token's
    scopes, on the project or its organization, as changing the end date
    directly does.
    A token bound elsewhere neither sees a project's requests nor files one.
    """

    def setUp(self):
        grant_end_date_requests()
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_PROJECT_END_DATE)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.SET_PROJECT_END_DATE
            )
        )
        self.owner = self.fixture.owner
        self.manager = self.fixture.manager
        self.other_customer = factories.CustomerFactory()
        self.other_customer.add_user(self.owner, CustomerRole.OWNER)
        self.other_project = factories.ProjectFactory(customer=self.other_customer)
        self.other_project.add_user(self.manager, ProjectRole.MANAGER)
        self.request = factories.ProjectEndDateChangeRequestFactory(
            project=self.project,
            created_by=self.manager,
            state=ReviewStates.PENDING,
            requested_end_date=(timezone.now() + timedelta(days=90)).date(),
        )

    def review(self, action):
        return self.client.post(
            factories.ProjectEndDateChangeRequestFactory.get_url(
                self.request, action=action
            )
        )

    def assert_still_pending(self):
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.PENDING)

    def test_token_without_end_date_scope_cannot_approve(self):
        use_pat(self.client, self.owner, PermissionEnum.LIST_PROJECTS.value)
        self.assertEqual(self.review("approve").status_code, status.HTTP_403_FORBIDDEN)
        self.assert_still_pending()

    def test_token_without_end_date_scope_cannot_reject(self):
        use_pat(self.client, self.owner, PermissionEnum.LIST_PROJECTS.value)
        self.assertEqual(self.review("reject").status_code, status.HTTP_403_FORBIDDEN)
        self.assert_still_pending()

    def test_token_bound_to_another_organization_cannot_approve(self):
        # Bound elsewhere, the token does not find the request at all.
        use_pat(
            self.client,
            self.owner,
            PermissionEnum.SET_PROJECT_END_DATE.value,
            bindings=[self.other_customer],
        )
        self.assertEqual(self.review("approve").status_code, status.HTTP_404_NOT_FOUND)
        self.assert_still_pending()

    def test_token_with_end_date_scope_can_approve(self):
        use_pat(self.client, self.owner, PermissionEnum.SET_PROJECT_END_DATE.value)
        self.assertEqual(self.review("approve").status_code, status.HTTP_200_OK)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.APPROVED)

    def test_token_bound_to_the_organization_can_reject(self):
        use_pat(
            self.client,
            self.owner,
            PermissionEnum.SET_PROJECT_END_DATE.value,
            bindings=[self.fixture.customer],
        )
        self.assertEqual(self.review("reject").status_code, status.HTTP_200_OK)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.REJECTED)

    def test_token_bound_to_the_project_can_approve(self):
        # As for changing the end date directly, a token bound to the project
        # may use its owner's role on the project's organization.
        use_pat(
            self.client,
            self.owner,
            PermissionEnum.SET_PROJECT_END_DATE.value,
            bindings=[self.project],
        )
        self.assertEqual(self.review("approve").status_code, status.HTTP_200_OK)

    def test_token_bound_elsewhere_does_not_list_or_retrieve_request(self):
        use_pat(
            self.client,
            self.manager,
            PermissionEnum.LIST_PROJECTS.value,
            bindings=[self.other_project],
        )
        response = self.client.get(
            factories.ProjectEndDateChangeRequestFactory.get_list_url()
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])
        response = self.client.get(
            factories.ProjectEndDateChangeRequestFactory.get_url(self.request)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_token_bound_to_the_project_lists_request(self):
        use_pat(
            self.client,
            self.manager,
            PermissionEnum.LIST_PROJECTS.value,
            bindings=[self.project],
        )
        response = self.client.get(
            factories.ProjectEndDateChangeRequestFactory.get_list_url()
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["uuid"] for item in response.data], [str(self.request.uuid)]
        )

    def test_token_bound_elsewhere_cannot_cancel_request(self):
        use_pat(
            self.client,
            self.manager,
            PermissionEnum.LIST_PROJECTS.value,
            bindings=[self.other_project],
        )
        self.assertEqual(self.review("cancel").status_code, status.HTTP_404_NOT_FOUND)
        self.assert_still_pending()

    def test_token_bound_to_the_project_can_cancel_request(self):
        use_pat(
            self.client,
            self.manager,
            PermissionEnum.LIST_PROJECTS.value,
            bindings=[self.project],
        )
        self.assertEqual(self.review("cancel").status_code, status.HTTP_200_OK)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.CANCELED)

    def create_request(self):
        return self.client.post(
            factories.ProjectEndDateChangeRequestFactory.get_list_url(),
            {
                "project": factories.ProjectFactory.get_url(self.project),
                "requested_end_date": self.new_end_date.isoformat(),
            },
        )

    @property
    def new_end_date(self):
        return (timezone.now() + timedelta(days=60)).date()

    def assert_not_created(self, response):
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project", response.data)
        self.assertFalse(
            models.ProjectEndDateChangeRequest.objects.filter(
                project=self.project, requested_end_date=self.new_end_date
            ).exists()
        )

    def test_token_bound_elsewhere_cannot_create_request(self):
        use_pat(
            self.client,
            self.manager,
            PermissionEnum.LIST_PROJECTS.value,
            bindings=[self.other_project],
        )
        self.assert_not_created(self.create_request())

    def test_token_bound_to_the_project_can_create_request(self):
        use_pat(
            self.client,
            self.manager,
            PermissionEnum.LIST_PROJECTS.value,
            bindings=[self.project],
        )
        self.assertEqual(self.create_request().status_code, status.HTTP_201_CREATED)

    def test_unbound_token_can_create_request(self):
        use_pat(self.client, self.manager, PermissionEnum.LIST_PROJECTS.value)
        self.assertEqual(self.create_request().status_code, status.HTTP_201_CREATED)

    def test_support_without_a_role_cannot_create_request(self):
        # Support sees every project, but only to read.
        self.client.force_authenticate(self.fixture.global_support)
        self.assert_not_created(self.create_request())

    def test_support_token_without_a_role_cannot_create_request(self):
        use_pat(
            self.client, self.fixture.global_support, PermissionEnum.LIST_PROJECTS.value
        )
        self.assert_not_created(self.create_request())

    def test_support_with_a_project_role_can_create_request(self):
        support = self.fixture.global_support
        self.project.add_user(support, ProjectRole.MEMBER)
        self.client.force_authenticate(support)
        self.assertEqual(self.create_request().status_code, status.HTTP_201_CREATED)


class ProjectEndDateChangeRequestCancelTest(test.APITestCase):
    def setUp(self):
        grant_end_date_requests()
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.request = factories.ProjectEndDateChangeRequestFactory(
            project=self.project,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
            requested_end_date=(timezone.now() + timedelta(days=90)).date(),
        )
        self.cancel_url = factories.ProjectEndDateChangeRequestFactory.get_url(
            self.request, action="cancel"
        )

    def test_creator_can_cancel_request(self):
        """User who created the request can cancel it."""
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.cancel_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("detail", response.data)
        self.assertTrue(response.data["detail"])

        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.CANCELED)

    def test_other_user_cannot_cancel_request(self):
        """User who did not create the request cannot cancel it."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.cancel_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.PENDING)

    def test_other_user_is_refused_before_the_state_is_checked(self):
        self.request.state = ReviewStates.APPROVED
        self.request.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.cancel_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_cancel_non_pending_request(self):
        """Cannot cancel request that is not pending."""
        self.request.state = ReviewStates.APPROVED
        self.request.save()
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.cancel_url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.APPROVED)


@override_settings(task_always_eager=True)
class ProjectEndDateChangeRequestNotificationTest(test.APITransactionTestCase):
    def setUp(self):
        grant_end_date_requests()

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
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_PROJECT_END_DATE)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.SET_PROJECT_END_DATE
            )
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
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_PROJECT_END_DATE)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.SET_PROJECT_END_DATE
            )
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
        grant_end_date_requests()
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


class ProjectEndDateUpdatedAtTest(test.APITestCase):
    def setUp(self):
        grant_end_date_requests()
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.url = factories.ProjectFactory.get_url(self.project)

    def test_end_date_updated_at_is_set_when_end_date_changes(self):
        self.assertIsNone(self.project.end_date_updated_at)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            self.url, {"end_date": "2030-01-01"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        self.assertIsNotNone(self.project.end_date_updated_at)

    def test_end_date_updated_at_not_changed_when_other_fields_change(self):
        self.assertIsNone(self.project.end_date_updated_at)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(self.url, {"name": "new name"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.end_date_updated_at)

    def test_end_date_updated_at_in_api_response(self):
        self.client.force_authenticate(self.fixture.staff)
        self.client.patch(self.url, {"end_date": "2030-01-01"}, format="json")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("end_date_updated_at", response.data)
        self.assertIsNotNone(response.data["end_date_updated_at"])

    def test_end_date_updated_at_set_on_approval(self):
        """end_date_updated_at is set when end_date changes via change request approval."""
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_PROJECT_END_DATE)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.SET_PROJECT_END_DATE
            )
        )
        change_request = factories.ProjectEndDateChangeRequestFactory(
            project=self.project,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
            requested_end_date=(timezone.now() + timedelta(days=90)).date(),
        )
        approve_url = factories.ProjectEndDateChangeRequestFactory.get_url(
            change_request, action="approve"
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(approve_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        self.assertIsNotNone(self.project.end_date_updated_at)
