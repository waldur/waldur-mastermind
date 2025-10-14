import uuid
from unittest import mock

import respx
from rest_framework import status, test

from waldur_core.structure.tests.factories import UserFactory
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    OrderStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    OrderFactory,
    ResourceFactory,
)
from waldur_mastermind.marketplace_remote.tests.dns_utils import (
    create_selective_dns_mock,
)


class SyncResourceViewTest(test.APITransactionTestCase):
    """Test cases for SyncResourceView endpoint"""

    def setUp(self):
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()

        self.fixture = ProjectFixture()
        self.staff_user = UserFactory(is_staff=True)
        self.regular_user = UserFactory()

        self.api_url = "https://remote-waldur.com"
        self.offering = OfferingFactory(
            type=REMOTE_OFFERING,
            secret_options={
                "api_url": self.api_url,
                "token": "valid_token",
            },
        )
        self.resource = ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            state=ResourceStates.OK,
            backend_id=uuid.uuid4().hex,
        )
        self.url = f"/api/remote-waldur-api/sync_resource/{self.resource.uuid.hex}/"

    def tearDown(self):
        respx.stop()
        self.dns_patcher.stop()
        super().tearDown()
        mock.patch.stopall()

    @mock.patch("waldur_mastermind.marketplace_remote.tasks.ResourcePullTask")
    def test_staff_user_can_sync_resource(self, mock_task):
        """Staff user should be able to trigger resource sync"""
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.return_value.apply_async.assert_called_once()

    def test_regular_user_cannot_sync_resource(self):
        """Regular user should not be able to trigger resource sync"""
        self.client.force_authenticate(self.regular_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_cannot_sync_resource(self):
        """Anonymous user should not be able to trigger resource sync"""
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sync_resource_not_found(self):
        """Should return 404 for non-existent resource"""
        self.client.force_authenticate(self.staff_user)
        non_existent_uuid = uuid.uuid4().hex
        url = f"/api/remote-waldur-api/sync_resource/{non_existent_uuid}/"

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_sync_terminated_resource(self):
        """Should return 400 for terminated resource"""
        self.client.force_authenticate(self.staff_user)
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("terminated", response.data[0].lower())

    def test_sync_updating_resource(self):
        """Should return 400 for resource in updating state"""
        self.client.force_authenticate(self.staff_user)
        self.resource.state = ResourceStates.UPDATING
        self.resource.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("updating", response.data[0].lower())

    @mock.patch("waldur_mastermind.marketplace_remote.tasks.ResourcePullTask")
    def test_task_called_with_correct_arguments(self, mock_task):
        """Should call task with serialized resource instance"""
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify apply_async was called with args and kwargs
        call_args = mock_task.return_value.apply_async.call_args
        self.assertIsNotNone(call_args)
        self.assertIn("args", call_args.kwargs)
        self.assertIn("kwargs", call_args.kwargs)
        self.assertEqual(call_args.kwargs["kwargs"], {})


class PullResourceRobotAccountsViewTest(test.APITransactionTestCase):
    """Test cases for PullResourceRobotAccountsView endpoint"""

    def setUp(self):
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()

        self.fixture = ProjectFixture()
        self.staff_user = UserFactory(is_staff=True)
        self.regular_user = UserFactory()

        self.api_url = "https://remote-waldur.com"
        self.offering = OfferingFactory(
            type=REMOTE_OFFERING,
            secret_options={
                "api_url": self.api_url,
                "token": "valid_token",
            },
        )
        self.resource = ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            state=ResourceStates.OK,
            backend_id=uuid.uuid4().hex,
        )
        self.url = f"/api/remote-waldur-api/pull_resource_robot_accounts/{self.resource.uuid.hex}/"

    def tearDown(self):
        respx.stop()
        self.dns_patcher.stop()
        super().tearDown()
        mock.patch.stopall()

    @mock.patch(
        "waldur_mastermind.marketplace_remote.tasks.ResourceRobotAccountPullTask"
    )
    def test_staff_user_can_pull_robot_accounts(self, mock_task):
        """Staff user should be able to trigger robot accounts pull"""
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.return_value.apply_async.assert_called_once()

    def test_regular_user_cannot_pull_robot_accounts(self):
        """Regular user should not be able to trigger robot accounts pull"""
        self.client.force_authenticate(self.regular_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_cannot_pull_robot_accounts(self):
        """Anonymous user should not be able to trigger robot accounts pull"""
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pull_robot_accounts_resource_not_found(self):
        """Should return 404 for non-existent resource"""
        self.client.force_authenticate(self.staff_user)
        non_existent_uuid = uuid.uuid4().hex
        url = (
            f"/api/remote-waldur-api/pull_resource_robot_accounts/{non_existent_uuid}/"
        )

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_pull_robot_accounts_terminated_resource(self):
        """Should return 400 for terminated resource"""
        self.client.force_authenticate(self.staff_user)
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("terminated", response.data[0].lower())

    def test_pull_robot_accounts_updating_resource(self):
        """Should return 400 for resource in updating state"""
        self.client.force_authenticate(self.staff_user)
        self.resource.state = ResourceStates.UPDATING
        self.resource.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("updating", response.data[0].lower())

    def test_pull_robot_accounts_non_remote_offering(self):
        """Should return 400 for non-remote offering"""
        self.client.force_authenticate(self.staff_user)
        self.offering.type = "OpenStack.Instance"
        self.offering.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not remote", response.data[0].lower())

    @mock.patch(
        "waldur_mastermind.marketplace_remote.tasks.ResourceRobotAccountPullTask"
    )
    def test_task_called_with_correct_arguments(self, mock_task):
        """Should call task with serialized resource instance"""
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify apply_async was called with args and kwargs
        call_args = mock_task.return_value.apply_async.call_args
        self.assertIsNotNone(call_args)
        self.assertIn("args", call_args.kwargs)
        self.assertIn("kwargs", call_args.kwargs)
        self.assertEqual(call_args.kwargs["kwargs"], {})


class PullOrderViewTest(test.APITransactionTestCase):
    """Test cases for PullOrderView endpoint"""

    def setUp(self):
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()

        self.fixture = ProjectFixture()

        self.api_url = "https://remote-waldur.com"
        self.offering = OfferingFactory(
            type=REMOTE_OFFERING,
            secret_options={
                "api_url": self.api_url,
                "token": "valid_token",
            },
        )
        self.resource = ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            backend_id=uuid.uuid4().hex,
        )
        self.order = OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            resource=self.resource,
            state=OrderStates.EXECUTING,
            backend_id=uuid.uuid4().hex,
        )
        self.url = f"/api/remote-waldur-api/pull_order/{self.order.uuid.hex}"

    def tearDown(self):
        respx.stop()
        self.dns_patcher.stop()
        super().tearDown()
        mock.patch.stopall()

    @mock.patch("waldur_mastermind.marketplace_remote.tasks.OrderPullTask")
    def test_any_user_can_pull_order(self, mock_task):
        """Any user (even anonymous) should be able to trigger order pull"""
        # Note: PullOrderView has permission_classes = [] so it's publicly accessible
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.return_value.apply_async.assert_called_once()

    def test_pull_order_not_found(self):
        """Should return 404 for non-existent order"""
        non_existent_uuid = uuid.uuid4().hex
        url = f"/api/remote-waldur-api/pull_order/{non_existent_uuid}"

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_pull_order_with_terminal_state(self):
        """Should return 404 for order in terminal state"""
        self.order.state = OrderStates.DONE
        self.order.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_pull_order_with_pending_state(self):
        """Should successfully pull order in pending state"""
        self.order.state = OrderStates.PENDING_CONSUMER
        self.order.save()

        with mock.patch(
            "waldur_mastermind.marketplace_remote.tasks.OrderPullTask"
        ) as mock_task:
            response = self.client.post(self.url)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            mock_task.return_value.apply_async.assert_called_once()

    def test_pull_order_non_remote_offering(self):
        """Should return 404 for non-remote offering order"""
        self.offering.type = "OpenStack.Instance"
        self.offering.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch("waldur_mastermind.marketplace_remote.tasks.OrderPullTask")
    def test_task_called_with_correct_arguments(self, mock_task):
        """Should call task with serialized order instance"""
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify apply_async was called with args and kwargs
        call_args = mock_task.return_value.apply_async.call_args
        self.assertIsNotNone(call_args)
        self.assertIn("args", call_args.kwargs)
        self.assertIn("kwargs", call_args.kwargs)
        self.assertEqual(call_args.kwargs["kwargs"], {})

    @mock.patch("waldur_mastermind.marketplace_remote.tasks.OrderPullTask")
    def test_authenticated_user_can_pull_order(self, mock_task):
        """Authenticated user should also be able to trigger order pull"""
        user = UserFactory()
        self.client.force_authenticate(user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.return_value.apply_async.assert_called_once()
