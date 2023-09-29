from unittest import mock

from django.test import TestCase

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures


class LogProjectSaveTest(TestCase):
    @mock.patch('waldur_core.structure.handlers.event_logger')
    def test_logger_called_once_on_project_create(self, logger_mock):
        new_project = factories.ProjectFactory()

        logger_mock.project.info.assert_called_once_with(
            'Project {project_name} has been created.',
            event_type='project_creation_succeeded',
            event_context={
                'project': new_project,
            },
        )

    def test_logger_called_once_on_project_name_update(self):
        new_project = factories.ProjectFactory()
        old_name = new_project.name

        with mock.patch('waldur_core.structure.handlers.event_logger') as logger_mock:
            new_project.name = 'new name'
            new_project.save()

            logger_mock.project.info.assert_called_once_with(
                "Project {project_name} has been updated. Name has been changed from '%s' to '%s'."
                % (
                    old_name,
                    new_project.name,
                ),
                event_type='project_update_succeeded',
                event_context={
                    'project': new_project,
                },
            )

    def test_logger_logs_project_name_and_description_when_updated(self):
        new_project = factories.ProjectFactory(description='description', name='name')

        with mock.patch('waldur_core.structure.handlers.event_logger') as logger_mock:
            new_project.name = 'new name'
            new_project.description = 'new description'
            new_project.save()

            expected_message = (
                'Project {project_name} has been updated.'
                " Description has been changed from 'description' to 'new description'."
                " Name has been changed from 'name' to 'new name'."
            )
            logger_mock.project.info.assert_called_once_with(
                expected_message,
                event_type='project_update_succeeded',
                event_context={
                    'project': new_project,
                },
            )


class LogRoleEventTest(TestCase):
    def test_logger_called_when_customer_role_is_granted(self):
        fixture = fixtures.CustomerFixture()

        owner = fixture.owner
        with mock.patch(
            'waldur_core.structure.handlers.event_logger.customer_role.info'
        ) as logger_mock:
            fixture.customer.add_user(fixture.user, CustomerRole.OWNER, owner)

            logger_mock.assert_called_once_with(
                mock.ANY,
                event_type='role_granted',
                event_context={
                    'customer': fixture.customer,
                    'user': fixture.owner,
                    'affected_user': fixture.user,
                    'structure_type': 'customer',
                    'role_name': 'owner',
                },
            )

    def test_logger_called_when_customer_role_is_revoked(self):
        fixture = fixtures.CustomerFixture()
        owner = fixture.owner

        with mock.patch(
            'waldur_core.structure.handlers.event_logger.customer_role.info'
        ) as logger_mock:
            fixture.customer.remove_user(owner, CustomerRole.OWNER, fixture.staff)

            logger_mock.assert_called_once_with(
                mock.ANY,
                event_type='role_revoked',
                event_context={
                    'customer': fixture.customer,
                    'user': fixture.staff,
                    'affected_user': fixture.owner,
                    'structure_type': 'customer',
                    'role_name': 'owner',
                },
            )

    def test_logger_called_when_project_role_is_granted(self):
        fixture = fixtures.ProjectFixture()

        with mock.patch(
            'waldur_core.structure.handlers.event_logger.project_role.info'
        ) as logger_mock:
            fixture.project.add_user(fixture.user, ProjectRole.MANAGER, fixture.owner)

            logger_mock.assert_called_once_with(
                mock.ANY,
                event_type='role_granted',
                event_context={
                    'project': fixture.project,
                    'user': fixture.owner,
                    'affected_user': fixture.user,
                    'structure_type': 'project',
                    'role_name': 'manager',
                },
            )

    def test_logger_called_when_project_role_is_revoked(self):
        fixture = fixtures.ProjectFixture()
        manager = fixture.manager

        with mock.patch(
            'waldur_core.structure.handlers.event_logger.project_role.info'
        ) as logger_mock:
            fixture.project.remove_user(manager, ProjectRole.MANAGER, fixture.owner)

            logger_mock.assert_called_once_with(
                mock.ANY,
                event_type='role_revoked',
                event_context={
                    'project': fixture.project,
                    'user': fixture.owner,
                    'affected_user': fixture.manager,
                    'structure_type': 'project',
                    'role_name': 'manager',
                },
            )
