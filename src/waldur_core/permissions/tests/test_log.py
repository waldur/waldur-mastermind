from unittest import mock

from django.test import TestCase
from rest_framework import test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures


class LogRoleEventTest(TestCase):
    def test_logger_called_when_customer_role_is_granted(self):
        fixture = fixtures.CustomerFixture()

        owner = fixture.owner
        with mock.patch(
            "waldur_core.structure.handlers.event_logger.user_role.info"
        ) as logger_mock:
            fixture.customer.add_user(fixture.user, CustomerRole.OWNER, owner)

            logger_mock.assert_called_once_with(
                mock.ANY,
                event_type="role_granted",
                event_context={
                    "scope": fixture.customer,
                    "scope_uuid": fixture.customer.uuid.hex,
                    "scope_name": fixture.customer.name,
                    "scope_type": "customer",
                    "customer": fixture.customer,
                    "user": fixture.owner,
                    "affected_user": fixture.user,
                    "role_name": CustomerRole.OWNER.name,
                },
            )

    def test_logger_called_when_customer_role_is_revoked(self):
        fixture = fixtures.CustomerFixture()
        owner = fixture.owner

        with mock.patch(
            "waldur_core.structure.handlers.event_logger.user_role.info"
        ) as logger_mock:
            fixture.customer.remove_user(owner, CustomerRole.OWNER, fixture.staff)

            logger_mock.assert_called_once_with(
                mock.ANY,
                event_type="role_revoked",
                event_context={
                    "scope": fixture.customer,
                    "scope_uuid": fixture.customer.uuid.hex,
                    "scope_name": fixture.customer.name,
                    "scope_type": "customer",
                    "customer": fixture.customer,
                    "user": fixture.staff,
                    "affected_user": fixture.owner,
                    "role_name": CustomerRole.OWNER.name,
                },
            )

    def test_logger_called_when_project_role_is_granted(self):
        fixture = fixtures.ProjectFixture()
        current_user = fixture.owner

        with mock.patch(
            "waldur_core.structure.handlers.event_logger.user_role.info"
        ) as logger_mock:
            fixture.project.add_user(fixture.user, ProjectRole.MANAGER, current_user)

            logger_mock.assert_called_once_with(
                mock.ANY,
                event_type="role_granted",
                event_context={
                    "scope": fixture.project,
                    "scope_uuid": fixture.project.uuid.hex,
                    "scope_name": fixture.project.name,
                    "scope_type": "project",
                    "customer": fixture.customer,
                    "user": current_user,
                    "affected_user": fixture.user,
                    "role_name": ProjectRole.MANAGER.name,
                },
            )

    def test_logger_called_when_project_role_is_revoked(self):
        fixture = fixtures.ProjectFixture()
        manager = fixture.manager
        current_user = fixture.owner

        with mock.patch(
            "waldur_core.structure.handlers.event_logger.user_role.info"
        ) as logger_mock:
            fixture.project.remove_user(manager, ProjectRole.MANAGER, current_user)

            logger_mock.assert_called_once_with(
                mock.ANY,
                event_type="role_revoked",
                event_context={
                    "scope": fixture.project,
                    "scope_uuid": fixture.project.uuid.hex,
                    "scope_name": fixture.project.name,
                    "scope_type": "project",
                    "customer": fixture.customer,
                    "user": current_user,
                    "affected_user": fixture.manager,
                    "role_name": ProjectRole.MANAGER.name,
                },
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

    def test_logger_called_when_subnet_created(self):
        with mock.patch(
            "waldur_core.structure.handlers.event_logger.access_subnet.info"
        ) as logger_mock:
            logger_mock.reset_mock()
            access_subnet = self.create_access_subnet()
            logger_mock.assert_called_once_with(
                mock.ANY,
                event_type="access_subnet_creation_succeeded",
                event_context={
                    "access_subnet": access_subnet,
                },
            )

    def test_logger_called_when_subnet_modified(self):
        access_subnet = self.create_access_subnet()
        url = factories.AccessSubnetFactory.get_url(access_subnet)

        with mock.patch(
            "waldur_core.structure.handlers.event_logger.access_subnet.info"
        ) as logger_mock:
            logger_mock.reset_mock()
            payload = {
                "inet": "192.168.1.1/32",
            }
            response = self.client.put(
                url,
                payload,
            )
            logger_mock.assert_called_once_with(
                mock.ANY,
                event_type="access_subnet_update_succeeded",  # TODO patch calls creation_succeeded but update_succeeded desired
                event_context={
                    "access_subnet": access_subnet,
                },
            )
            return response

    def test_logger_called_when_subnet_deleted(self):
        access_subnet = self.create_access_subnet()
        url = factories.AccessSubnetFactory.get_url(access_subnet)

        with mock.patch(
            "waldur_core.structure.handlers.event_logger.access_subnet.info"
        ) as logger_mock:
            logger_mock.reset_mock()
            response = self.client.delete(url)
            logger_mock.assert_called_with(
                mock.ANY,
                event_type="access_subnet_deletion_succeeded",
                event_context=mock.ANY,
            )
        return response

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
