from unittest import mock

from django.core import mail
from rest_framework import status, test

from waldur_core.core.enums import ReviewStates
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, tasks
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.tests import factories, fixtures


class ResourceLimitChangeRequestCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = models.Resource.States.OK
        self.resource.save()
        self.list_url = factories.ResourceLimitChangeRequestFactory.get_list_url()

    def get_valid_payload(self):
        return {
            "resource": self.resource.uuid.hex,
            "requested_limits": {"storage": 500},
        }

    def test_project_member_without_update_permission_can_create_request(self):
        """Project member without UPDATE_RESOURCE_LIMITS can create request."""
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(
            self.list_url, self.get_valid_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["state"], "pending")
        self.assertEqual(
            models.ResourceLimitChangeRequest.objects.filter(
                resource=self.resource, created_by=self.fixture.manager
            ).count(),
            1,
        )

    def test_user_with_update_resource_limits_permission_cannot_create_request(self):
        """User with UPDATE_RESOURCE_LIMITS should update directly, not create request."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.UPDATE_RESOURCE_LIMITS
            )
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.list_url, self.get_valid_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.ResourceLimitChangeRequest.objects.filter(
                resource=self.resource
            ).exists()
        )

    def test_staff_user_cannot_create_request(self):
        """Staff users should update directly, not create a request."""
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.fixture.project.add_user(staff_user, ProjectRole.MEMBER)
        self.client.force_authenticate(staff_user)
        response = self.client.post(
            self.list_url, self.get_valid_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.ResourceLimitChangeRequest.objects.filter(
                resource=self.resource
            ).exists()
        )

    def test_support_user_cannot_create_request(self):
        """Support users have full visibility and should update directly."""
        support_user = structure_factories.UserFactory(is_support=True)
        self.client.force_authenticate(support_user)
        response = self.client.post(
            self.list_url, self.get_valid_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.ResourceLimitChangeRequest.objects.filter(
                resource=self.resource
            ).exists()
        )

    def test_unauthenticated_user_cannot_create_request(self):
        response = self.client.post(
            self.list_url, self.get_valid_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cannot_create_duplicate_pending_request_for_same_resource(self):
        """Only one pending request per resource allowed."""
        factories.ResourceLimitChangeRequestFactory(
            resource=self.resource,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
        )
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(
            self.list_url, self.get_valid_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("pending", str(response.data).lower())


class ResourceLimitChangeRequestListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.request = factories.ResourceLimitChangeRequestFactory(
            resource=self.resource,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
        )
        self.list_url = factories.ResourceLimitChangeRequestFactory.get_list_url()

    def test_owner_can_list_requests_for_organization_resources(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.UPDATE_RESOURCE_LIMITS
            )
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [r["uuid"] for r in response.data]
        self.assertIn(str(self.request.uuid), uuids)

    def test_creator_can_see_own_requests(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(
            self.list_url, {"resource_uuid": self.resource.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 1)

    def test_creator_without_membership_can_see_own_request(self):
        """Creator keeps visibility of their request after losing project membership."""
        outsider = structure_factories.UserFactory()
        outsider_request = factories.ResourceLimitChangeRequestFactory(
            resource=self.resource,
            created_by=outsider,
            state=ReviewStates.PENDING,
        )
        self.client.force_authenticate(outsider)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [r["uuid"] for r in response.data]
        self.assertIn(str(outsider_request.uuid), uuids)

    def test_filter_by_resource_uuid(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(
            self.list_url, {"resource_uuid": self.resource.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for item in response.data:
            self.assertEqual(item["resource_uuid"], self.resource.uuid.hex)

    def test_filter_by_state(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(self.list_url, {"state": "pending"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ResourceLimitChangeRequestApproveRejectTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = models.Resource.States.OK
        self.resource.save()
        self.resource.offering.shared = True
        self.resource.offering.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.UPDATE_RESOURCE_LIMITS
            )
        )
        self.request = factories.ResourceLimitChangeRequestFactory(
            resource=self.resource,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
            requested_limits={"storage": 500},
        )
        self.approve_url = factories.ResourceLimitChangeRequestFactory.get_url(
            self.request, action="approve"
        )
        self.reject_url = factories.ResourceLimitChangeRequestFactory.get_url(
            self.request, action="reject"
        )

    def test_owner_can_approve_request_and_order_is_created(self):
        """Organization owner can approve request and marketplace order is created."""
        factories.OfferingComponentFactory(
            offering=self.resource.offering,
            type="storage",
            billing_type=BillingTypes.LIMIT,
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url, {"comment": "Approved"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("order_uuid", response.data)

        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.APPROVED)
        self.assertEqual(self.request.reviewed_by, self.fixture.owner)

        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.limits, {"storage": 500})
        self.assertEqual(order.resource, self.resource)

    def test_owner_can_reject_request(self):
        """Organization owner can reject request."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.reject_url, {"comment": "Rejected"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.REJECTED)

    def test_member_without_permission_cannot_approve(self):
        """Project member without UPDATE_RESOURCE_LIMITS cannot see nor approve the request."""
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(self.approve_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

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

    def test_cannot_approve_when_resource_is_not_ok(self):
        """Approval is rejected when the resource is not in OK state."""
        self.resource.state = models.Resource.States.ERRED
        self.resource.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url, {"comment": "Approved"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.PENDING)

    def test_cannot_approve_when_requested_limits_equal_current_limits(self):
        """Approval is rejected when requested limits match the resource limits."""
        self.resource.limits = {"storage": 500}
        self.resource.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url, {"comment": "Approved"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.PENDING)

    def test_cannot_approve_when_requested_limits_exceed_component_maximum(self):
        """Approval is rejected when a requested limit exceeds the component maximum."""
        factories.OfferingComponentFactory(
            offering=self.resource.offering,
            type="storage",
            billing_type=BillingTypes.LIMIT,
            max_value=100,
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url, {"comment": "Approved"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.PENDING)


class ResourceLimitChangeRequestCancelTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.request = factories.ResourceLimitChangeRequestFactory(
            resource=self.resource,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
        )
        self.cancel_url = factories.ResourceLimitChangeRequestFactory.get_url(
            self.request, action="cancel"
        )

    def test_creator_can_cancel_request(self):
        """User who created the request can cancel it."""
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.cancel_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.CANCELED)

    def test_other_user_cannot_cancel_request(self):
        """User who did not create the request cannot see nor cancel it."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.cancel_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.PENDING)

    def test_creator_without_membership_can_cancel_request(self):
        """Creator can cancel their request after losing project membership."""
        outsider = structure_factories.UserFactory()
        outsider_request = factories.ResourceLimitChangeRequestFactory(
            resource=self.resource,
            created_by=outsider,
            state=ReviewStates.PENDING,
        )
        cancel_url = factories.ResourceLimitChangeRequestFactory.get_url(
            outsider_request, action="cancel"
        )
        self.client.force_authenticate(outsider)
        response = self.client.post(cancel_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        outsider_request.refresh_from_db()
        self.assertEqual(outsider_request.state, ReviewStates.CANCELED)

    def test_cannot_cancel_non_pending_request(self):
        """Cannot cancel request that is not pending."""
        self.request.state = ReviewStates.APPROVED
        self.request.save()
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.cancel_url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        self.request.refresh_from_db()
        self.assertEqual(self.request.state, ReviewStates.APPROVED)


class ResourceLimitChangeRequestEventTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = models.Resource.States.OK
        self.resource.save()

    def test_event_created_when_request_created(self):
        """Event is logged when request is created via API."""
        initial_count = Event.objects.filter(
            event_type="marketplace_resource_limit_change_request_created"
        ).count()
        list_url = factories.ResourceLimitChangeRequestFactory.get_list_url()
        payload = {
            "resource": self.resource.uuid.hex,
            "requested_limits": {"storage": 500},
        }
        self.client.force_authenticate(self.fixture.manager)
        self.client.post(list_url, payload, format="json")
        self.assertEqual(
            Event.objects.filter(
                event_type="marketplace_resource_limit_change_request_created"
            ).count(),
            initial_count + 1,
        )


class ResourceLimitChangeRequestNotificationTest(test.APITestCase):
    @mock.patch(
        "waldur_mastermind.marketplace.tasks.send_resource_limit_change_request_notification"
    )
    def test_notification_sent_when_request_created(self, mock_task):
        """Notification task is scheduled when request is created."""
        mock_task.delay = mock.Mock()
        fixture = fixtures.MarketplaceFixture()
        resource = fixture.resource
        resource.state = models.Resource.States.OK
        resource.save()
        list_url = factories.ResourceLimitChangeRequestFactory.get_list_url()
        payload = {
            "resource": resource.uuid.hex,
            "requested_limits": {"storage": 500},
        }
        self.client.force_authenticate(fixture.manager)
        response = self.client.post(list_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_task.delay.assert_called_once()
        call_args = mock_task.delay.call_args[0]
        self.assertEqual(len(call_args[0]), 32)  # UUID hex string

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.send_resource_limit_change_request_approved_notification"
    )
    def test_notification_sent_to_requester_when_request_approved(self, mock_task):
        """Approved notification task is scheduled when request is approved."""
        mock_task.delay = mock.Mock()
        fixture = fixtures.MarketplaceFixture()
        resource = fixture.resource
        resource.state = models.Resource.States.OK
        resource.save()
        resource.offering.shared = True
        resource.offering.save()
        factories.OfferingComponentFactory(
            offering=resource.offering,
            type="storage",
            billing_type=BillingTypes.LIMIT,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.UPDATE_RESOURCE_LIMITS
            )
        )
        request = factories.ResourceLimitChangeRequestFactory(
            resource=resource,
            created_by=fixture.manager,
            state=ReviewStates.PENDING,
            requested_limits={"storage": 500},
        )
        approve_url = factories.ResourceLimitChangeRequestFactory.get_url(
            request, action="approve"
        )
        self.client.force_authenticate(fixture.owner)
        response = self.client.post(approve_url, {"comment": "Approved"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.delay.assert_called_once_with(request.uuid.hex)

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.send_resource_limit_change_request_rejected_notification"
    )
    def test_notification_sent_to_requester_when_request_rejected(self, mock_task):
        """Rejected notification task is scheduled when request is rejected."""
        mock_task.delay = mock.Mock()
        fixture = fixtures.MarketplaceFixture()
        resource = fixture.resource
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        self.addCleanup(
            lambda: CustomerRole.OWNER.delete_permission(
                PermissionEnum.UPDATE_RESOURCE_LIMITS
            )
        )
        request = factories.ResourceLimitChangeRequestFactory(
            resource=resource,
            created_by=fixture.manager,
            state=ReviewStates.PENDING,
        )
        reject_url = factories.ResourceLimitChangeRequestFactory.get_url(
            request, action="reject"
        )
        self.client.force_authenticate(fixture.owner)
        response = self.client.post(reject_url, {"comment": "Rejected"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.delay.assert_called_once_with(request.uuid.hex)


class ResourceLimitChangeRequestNotificationDispatchTest(test.APITestCase):
    """The notification tasks must actually deliver email via broadcast_mail.

    broadcast_mail() silently returns unless a Notification row exists for the
    key, so these tests guard against the notifications being unregistered.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # Materialize the owner so the customer has an owner email to notify.
        self.owner = self.fixture.owner
        self.request = factories.ResourceLimitChangeRequestFactory(
            resource=self.fixture.resource,
            created_by=self.fixture.manager,
            state=ReviewStates.PENDING,
            requested_limits={"storage": 500},
        )

    def test_created_notification_email_is_sent_to_owners(self):
        structure_factories.NotificationFactory(
            key="marketplace.notification_resource_limit_change_request_created"
        )
        tasks.send_resource_limit_change_request_notification(self.request.uuid.hex)
        recipients = {addr for message in mail.outbox for addr in message.to}
        self.assertIn(self.fixture.owner.email, recipients)

    def test_approved_notification_email_is_sent_to_requester(self):
        structure_factories.NotificationFactory(
            key="marketplace.notification_resource_limit_change_request_approved"
        )
        tasks.send_resource_limit_change_request_approved_notification(
            self.request.uuid.hex
        )
        recipients = {addr for message in mail.outbox for addr in message.to}
        self.assertIn(self.fixture.manager.email, recipients)

    def test_rejected_notification_email_is_sent_to_requester(self):
        structure_factories.NotificationFactory(
            key="marketplace.notification_resource_limit_change_request_rejected"
        )
        tasks.send_resource_limit_change_request_rejected_notification(
            self.request.uuid.hex
        )
        recipients = {addr for message in mail.outbox for addr in message.to}
        self.assertIn(self.fixture.manager.email, recipients)
