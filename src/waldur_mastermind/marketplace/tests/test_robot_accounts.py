from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.utils import add_permission
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class RobotAccountTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.CREATE_RESOURCE_ROBOT_ACCOUNT
        )
        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT
        )
        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.DELETE_RESOURCE_ROBOT_ACCOUNT
        )

        add_permission(
            RoleEnum.CUSTOMER_MANAGER, PermissionEnum.CREATE_RESOURCE_ROBOT_ACCOUNT
        )
        add_permission(
            RoleEnum.CUSTOMER_MANAGER, PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT
        )
        add_permission(
            RoleEnum.CUSTOMER_MANAGER, PermissionEnum.DELETE_RESOURCE_ROBOT_ACCOUNT
        )

    @data('staff', 'service_manager', 'service_owner')
    def test_authorized_user_can_create_robot_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.RobotAccountFactory.get_list_url()
        resource_url = factories.ResourceFactory.get_url(self.fixture.resource)
        response = self.client.post(url, {'resource': resource_url, 'type': 'cicd'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_create_robot_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.RobotAccountFactory.get_list_url()
        resource_url = factories.ResourceFactory.get_url(self.fixture.resource)
        response = self.client.post(url, {'resource': resource_url, 'type': 'cicd'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        'staff',
        'service_manager',
        'service_owner',
        'customer_support',
        'admin',
        'manager',
    )
    def test_authorized_user_can_get_robot_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.RobotAccountFactory(resource=self.fixture.resource)
        url = factories.RobotAccountFactory.get_url(account)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data('staff', 'service_manager', 'service_owner')
    def test_authorized_user_can_update_robot_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.RobotAccountFactory(resource=self.fixture.resource)
        url = factories.RobotAccountFactory.get_url(account)

        response = self.client.patch(url, {'username': 'foo'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        account.refresh_from_db()
        self.assertEqual(account.username, 'foo')

    @data('admin', 'manager')
    def test_unauthorized_user_can_not_update_robot_account(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        account = factories.RobotAccountFactory(resource=self.fixture.resource)
        url = factories.RobotAccountFactory.get_url(account)

        response = self.client.patch(url, {'username': 'foo'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
