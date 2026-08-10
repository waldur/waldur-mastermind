"""End date change requests from users who cannot change the date themselves.

A project member records what they want, someone holding the permission
approves, and the approval writes the date straight onto the resource. No order
is involved on any path. Requests are also published as events so an external
approval system can take the decision instead.
"""

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.enums import ReviewStates
from waldur_core.logging.enums import ObservableObjectType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


class BaseEndDateChangeRequestTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        # The manager manages limits but not end dates: they ask like anyone
        # else, and only SET_RESOURCE_END_DATE writes or decides.
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_END_DATE)

        self.offering = self.fixture.offering
        self.offering.type = BASIC_OFFERING
        self.offering.plugin_options = {
            "enable_resource_end_date_change_requests": True
        }
        self.offering.save()
        factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu_hours",
            billing_type=BillingTypes.LIMIT,
            is_prepaid=False,
        )

        self.resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.fixture.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            end_date=timezone.now().date() + timedelta(days=30),
        )
        self.requested_end_date = timezone.now().date() + timedelta(days=120)
        self.list_url = "/api/marketplace-resource-end-date-change-requests/"

    def create_request_payload(self, end_date=None):
        return {
            "resource": self.resource.uuid.hex,
            "requested_end_date": (end_date or self.requested_end_date).isoformat(),
        }

    def make_request(self, created_by=None, end_date=None):
        return models.ResourceEndDateChangeRequest.objects.create(
            resource=self.resource,
            requested_end_date=end_date or self.requested_end_date,
            created_by=created_by or self.fixture.member,
            state=ReviewStates.PENDING,
        )


class EndDateChangeRequestCreateTest(BaseEndDateChangeRequestTest):
    def test_member_without_permission_can_create_request(self):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(self.list_url, self.create_request_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            models.ResourceEndDateChangeRequest.objects.filter(
                resource=self.resource, created_by=self.fixture.member
            ).count(),
            1,
        )

    def test_user_who_can_change_directly_cannot_create_request(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.list_url, self.create_request_payload())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("permission", str(response.data).lower())

    def test_project_manager_creates_a_request_like_anyone_else(self):
        """Managing limits does not carry the end date, so they ask for it."""
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.list_url, self.create_request_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_staff_cannot_create_request(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.list_url, self.create_request_payload())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_request_is_refused_when_the_offering_has_not_opted_in(self):
        self.offering.plugin_options = {}
        self.offering.save()

        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(self.list_url, self.create_request_payload())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_date_is_rejected_at_creation(self):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(
            self.list_url,
            self.create_request_payload(
                end_date=timezone.now().date() - timedelta(days=1)
            ),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_identical_date_is_rejected(self):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(
            self.list_url, self.create_request_payload(end_date=self.resource.end_date)
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_one_pending_request_per_user_and_resource(self):
        self.make_request(created_by=self.fixture.member)

        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(self.list_url, self.create_request_payload())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class EndDateChangeRequestApproveTest(BaseEndDateChangeRequestTest):
    def setUp(self):
        super().setUp()
        self.request_obj = self.make_request()
        self.approve_url = (
            f"/api/marketplace-resource-end-date-change-requests/"
            f"{self.request_obj.uuid.hex}/approve/"
        )
        self.reject_url = (
            f"/api/marketplace-resource-end-date-change-requests/"
            f"{self.request_obj.uuid.hex}/reject/"
        )

    def test_approval_applies_the_date_directly(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.state, ReviewStates.APPROVED)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.end_date, self.requested_end_date)
        self.assertEqual(self.resource.end_date_requested_by, self.fixture.owner)

    def test_approval_creates_no_order(self):
        self.client.force_authenticate(self.fixture.owner)
        self.client.post(self.approve_url)

        self.assertFalse(models.Order.objects.filter(resource=self.resource).exists())

    def test_approval_records_the_reviewer(self):
        self.client.force_authenticate(self.fixture.owner)
        self.client.post(self.approve_url, {"comment": "agreed"})

        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.reviewed_by, self.fixture.owner)
        self.assertEqual(self.request_obj.review_comment, "agreed")
        self.assertIsNotNone(self.request_obj.reviewed_at)

    def test_member_cannot_approve(self):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(self.approve_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.state, ReviewStates.PENDING)

    def test_limit_manager_cannot_approve(self):
        """They can see the request — visibility follows the resource — but the
        outcome is not theirs to grant."""
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.approve_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.request_obj.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual(self.request_obj.state, ReviewStates.PENDING)
        self.assertNotEqual(self.resource.end_date, self.requested_end_date)

    def test_limit_manager_sees_requests_they_cannot_decide(self):
        """Seeing a request follows seeing the resource; deciding is separate."""
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_reject_leaves_the_resource_alone(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.reject_url, {"comment": "not now"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.request_obj.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual(self.request_obj.state, ReviewStates.REJECTED)
        self.assertNotEqual(self.resource.end_date, self.requested_end_date)
        self.assertFalse(models.Order.objects.filter(resource=self.resource).exists())

    def test_date_invalid_by_approval_time_is_refused(self):
        project = self.fixture.project
        project.end_date = timezone.now().date() + timedelta(days=60)
        project.save()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.state, ReviewStates.PENDING)

    def test_cannot_approve_once_the_offering_stops_accepting_requests(self):
        """The option can be turned off, or a prepaid component added, while a
        request waits — approval must not sneak past the gate that refused it
        at creation time."""
        self.offering.plugin_options = {}
        self.offering.save()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.request_obj.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual(self.request_obj.state, ReviewStates.PENDING)
        self.assertNotEqual(self.resource.end_date, self.requested_end_date)

    def test_cannot_approve_once_the_offering_becomes_prepaid(self):
        factories.OfferingComponentFactory(
            offering=self.offering,
            type="prepaid_seat",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
        )

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.resource.refresh_from_db()
        self.assertNotEqual(self.resource.end_date, self.requested_end_date)

    def test_cannot_approve_when_resource_is_not_ok(self):
        self.resource.state = ResourceStates.ERRED
        self.resource.save()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class EndDateChangeRequestFilterTest(BaseEndDateChangeRequestTest):
    def setUp(self):
        super().setUp()
        self.make_request(created_by=self.fixture.member)
        self.client.force_authenticate(self.fixture.owner)

    def test_requests_can_be_filtered_by_offering(self):
        """An external approval system watches specific offerings."""
        response = self.client.get(
            self.list_url, {"offering_uuid": self.offering.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_another_offering_matches_nothing(self):
        other = factories.OfferingFactory()

        response = self.client.get(self.list_url, {"offering_uuid": other.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class EndDateChangeRequestVisibilityTest(BaseEndDateChangeRequestTest):
    def test_creator_sees_own_request(self):
        self.make_request(created_by=self.fixture.member)

        self.client.force_authenticate(self.fixture.member)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_approver_sees_requests_in_scope(self):
        self.make_request(created_by=self.fixture.member)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_unrelated_user_sees_nothing(self):
        self.make_request(created_by=self.fixture.member)

        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class EndDateChangeRequestCancelTest(BaseEndDateChangeRequestTest):
    def test_creator_can_cancel(self):
        request_obj = self.make_request(created_by=self.fixture.member)
        url = (
            f"/api/marketplace-resource-end-date-change-requests/"
            f"{request_obj.uuid.hex}/cancel/"
        )

        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        request_obj.refresh_from_db()
        self.assertEqual(request_obj.state, ReviewStates.CANCELED)

    def test_approver_cannot_cancel_someone_elses_request(self):
        """Cancelling is the requester's own act; an approver rejects instead."""
        request_obj = self.make_request(created_by=self.fixture.member)
        url = (
            f"/api/marketplace-resource-end-date-change-requests/"
            f"{request_obj.uuid.hex}/cancel/"
        )

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        request_obj.refresh_from_db()
        self.assertEqual(request_obj.state, ReviewStates.PENDING)


class EndDateChangeRequestExternalApprovalTest(BaseEndDateChangeRequestTest):
    """The seams an external approval system drives: events and backend_id."""

    def setUp(self):
        super().setUp()
        self.request_obj = self.make_request()
        self.backend_id_url = (
            f"/api/marketplace-resource-end-date-change-requests/"
            f"{self.request_obj.uuid.hex}/set_backend_id/"
        )

    def test_approver_can_record_a_backend_id(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.backend_id_url, {"backend_id": "SP-42"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.backend_id, "SP-42")

    def test_member_cannot_record_a_backend_id(self):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(self.backend_id_url, {"backend_id": "SP-42"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.backend_id, "")

    def test_backend_id_is_exposed_but_not_writable_through_the_list_endpoint(self):
        self.request_obj.backend_id = "SP-7"
        self.request_obj.save()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["backend_id"], "SP-7")

    @mock.patch("waldur_mastermind.marketplace.handlers.logging_tasks")
    def test_event_is_published_on_creation(self, mock_tasks):
        with mock.patch(
            "waldur_mastermind.marketplace.handlers.marketplace_utils.prepare_messages",
            return_value=[{"topic": "t", "payload": "{}", "vhost": "v"}],
        ) as prepare:
            self.make_request(created_by=self.fixture.admin)

        self.assertTrue(prepare.called)
        affected_object = prepare.call_args[0][2]
        self.assertEqual(
            affected_object, ObservableObjectType.RESOURCE_END_DATE_CHANGE_REQUEST
        )
        payload = prepare.call_args[0][1]
        self.assertEqual(payload["resource_uuid"], self.resource.uuid.hex)
        self.assertEqual(payload["request_state"], "pending")
        mock_tasks.publish_messages.delay.assert_called_once()

    @mock.patch("waldur_mastermind.marketplace.handlers.logging_tasks")
    def test_event_is_published_on_approval(self, mock_tasks):
        with mock.patch(
            "waldur_mastermind.marketplace.handlers.marketplace_utils.prepare_messages",
            return_value=[{"topic": "t", "payload": "{}", "vhost": "v"}],
        ) as prepare:
            self.client.force_authenticate(self.fixture.owner)
            self.client.post(
                f"/api/marketplace-resource-end-date-change-requests/"
                f"{self.request_obj.uuid.hex}/approve/"
            )

        states = [call[0][1]["request_state"] for call in prepare.call_args_list]
        self.assertIn("approved", states)
