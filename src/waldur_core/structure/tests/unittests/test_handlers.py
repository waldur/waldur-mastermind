from unittest import mock

from django.test import TestCase

from waldur_core.logging.enums import EventType
from waldur_core.structure.tests import factories


class LogProjectSaveTest(TestCase):
    def test_logger_called_once_on_project_create(self):
        customer = factories.CustomerFactory()
        with mock.patch("waldur_core.logging.event_logger.emit") as logger_mock:
            new_project = factories.ProjectFactory(customer=customer)

            logger_mock.assert_called_once_with(
                "Project {project_name} has been created.",
                event_type=EventType.PROJECT_CREATION_SUCCEEDED,
                event_context={
                    "project": new_project,
                },
                scopes=[new_project, new_project.customer],
            )

    def test_logger_called_once_on_project_name_update(self):
        new_project = factories.ProjectFactory()
        old_name = new_project.name

        with mock.patch("waldur_core.logging.event_logger.emit") as logger_mock:
            new_project.name = "new name"
            new_project.save()

            logger_mock.assert_called_once_with(
                f"Project {{project_name}} has been updated. "
                f"Name has been changed from '{old_name}' to '{new_project.name}'.",
                event_type=EventType.PROJECT_UPDATE_SUCCEEDED,
                event_context={
                    "project": new_project,
                },
                scopes=[new_project, new_project.customer],
            )

    def test_logger_logs_project_name_and_description_when_updated(self):
        new_project = factories.ProjectFactory(description="description", name="name")

        with mock.patch("waldur_core.logging.event_logger.emit") as logger_mock:
            new_project.name = "new name"
            new_project.description = "new description"
            new_project.save()

            expected_message = (
                "Project {project_name} has been updated."
                " Description has been changed from 'description' to 'new description'."
                " Name has been changed from 'name' to 'new name'."
            )
            logger_mock.assert_called_once_with(
                expected_message,
                event_type=EventType.PROJECT_UPDATE_SUCCEEDED,
                event_context={
                    "project": new_project,
                },
                scopes=[new_project, new_project.customer],
            )
