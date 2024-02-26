from unittest import mock

from django.test import TestCase

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import fixtures


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
