from unittest import mock

from django.test import TestCase
from rest_framework import test

from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures


class LogRoleEventTest(TestCase):
    def setUp(self):
        self._logger_mock = mock.patch("waldur_core.logging.event_logger.emit")
        self.logger_mock = self._logger_mock.start()
        self.addCleanup(self._logger_mock.stop)

    def test_logger_called_when_customer_role_is_granted(self):
        fixture = fixtures.CustomerFixture()

        owner = fixture.owner
        self.logger_mock.reset_mock()
        fixture.customer.add_user(fixture.user, CustomerRole.OWNER, owner)

        self.logger_mock.assert_any_call(
            mock.ANY,
            event_type=EventType.ROLE_GRANTED,
            event_context=mock.ANY,
            scopes=[fixture.customer, fixture.customer],
        )

    def test_logger_called_when_customer_role_is_revoked(self):
        fixture = fixtures.CustomerFixture()
        owner = fixture.owner

        self.logger_mock.reset_mock()
        fixture.customer.remove_user(owner, CustomerRole.OWNER, fixture.staff)

        self.logger_mock.assert_any_call(
            mock.ANY,
            event_type=EventType.ROLE_REVOKED,
            event_context=mock.ANY,
            scopes=[fixture.customer, fixture.customer],
        )

    def test_logger_called_when_project_role_is_granted(self):
        fixture = fixtures.ProjectFixture()
        current_user = fixture.owner

        self.logger_mock.reset_mock()
        fixture.project.add_user(fixture.user, ProjectRole.MANAGER, current_user)

        self.logger_mock.assert_any_call(
            mock.ANY,
            event_type=EventType.ROLE_GRANTED,
            event_context=mock.ANY,
            scopes=[fixture.project, fixture.customer],
        )

    def test_logger_called_when_project_role_is_revoked(self):
        fixture = fixtures.ProjectFixture()
        manager = fixture.manager
        current_user = fixture.owner

        self.logger_mock.reset_mock()
        fixture.project.remove_user(manager, ProjectRole.MANAGER, current_user)

        self.logger_mock.assert_called_once_with(
            mock.ANY,
            event_type=EventType.ROLE_REVOKED,
            event_context=mock.ANY,
            scopes=[fixture.project, fixture.customer],
        )


class AccessSubnetCreateModifyDelete(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.client.force_authenticate(user=self.fixture.owner)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ACCESS_SUBNET)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_ACCESS_SUBNET)
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_ACCESS_SUBNET)
        self.customer = self.fixture.customer
        self.customer_url = factories.CustomerFactory.get_url(
            customer=self.fixture.customer
        )
        self._logger_mock = mock.patch("waldur_core.logging.event_logger.emit")
        self.logger_mock = self._logger_mock.start()
        self.addCleanup(self._logger_mock.stop)

    def test_logger_called_when_subnet_created(self):
        self.logger_mock.reset_mock()
        access_subnet = self.create_access_subnet()
        self.logger_mock.assert_called_once_with(
            mock.ANY,
            event_type=EventType.ACCESS_SUBNET_CREATION_SUCCEEDED,
            event_context={
                "access_subnet": access_subnet,
            },
            scopes=[access_subnet, access_subnet.customer],
        )

    def test_logger_called_when_subnet_modified(self):
        access_subnet = self.create_access_subnet()
        url = factories.AccessSubnetFactory.get_url(access_subnet)

        self.logger_mock.reset_mock()
        self.client.put(url, {"inet": "192.168.1.1/32"})
        self.logger_mock.assert_called_once_with(
            mock.ANY,
            event_type=EventType.ACCESS_SUBNET_UPDATE_SUCCEEDED,  # TODO patch calls creation_succeeded but update_succeeded desired
            event_context={
                "access_subnet": access_subnet,
            },
            scopes=[access_subnet, access_subnet.customer],
        )

    def test_logger_called_when_subnet_deleted(self):
        access_subnet = self.create_access_subnet()
        url = factories.AccessSubnetFactory.get_url(access_subnet)

        self.client.delete(url)
        self.logger_mock.assert_called_with(
            mock.ANY,
            event_type=EventType.ACCESS_SUBNET_DELETION_SUCCEEDED,
            event_context=mock.ANY,
            scopes=mock.ANY,
        )

    def create_access_subnet(self):
        url = factories.AccessSubnetFactory.get_list_url()
        payload = {
            "customer": self.customer_url,
            "inet": "192.168.1.0/32",
            "description": "",
        }
        response = self.client.post(
            url,
            payload,
        )
        return response.data.serializer.instance
