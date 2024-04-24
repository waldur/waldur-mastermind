from unittest import mock

from django.test import TestCase

from waldur_core.structure.tests import factories


class LogProjectSaveTest(TestCase):
    @mock.patch("waldur_core.structure.handlers.event_logger")
    def test_logger_called_once_on_project_create(self, logger_mock):
        new_project = factories.ProjectFactory()

        logger_mock.project.info.assert_called_once_with(
            "Project {project_name} has been created.",
            event_type="project_creation_succeeded",
            event_context={
                "project": new_project,
            },
        )

    def test_logger_called_once_on_project_name_update(self):
        new_project = factories.ProjectFactory()
        old_name = new_project.name

        with mock.patch("waldur_core.structure.handlers.event_logger") as logger_mock:
            new_project.name = "new name"
            new_project.save()

            logger_mock.project.info.assert_called_once_with(
                f"Project {{project_name}} has been updated. Name has been changed from '{old_name}' to '{new_project.name}'.",
                event_type="project_update_succeeded",
                event_context={
                    "project": new_project,
                },
            )

    def test_logger_logs_project_name_and_description_when_updated(self):
        new_project = factories.ProjectFactory(description="description", name="name")

        with mock.patch("waldur_core.structure.handlers.event_logger") as logger_mock:
            new_project.name = "new name"
            new_project.description = "new description"
            new_project.save()

            expected_message = (
                "Project {project_name} has been updated."
                " Description has been changed from 'description' to 'new description'."
                " Name has been changed from 'name' to 'new name'."
            )
            logger_mock.project.info.assert_called_once_with(
                expected_message,
                event_type="project_update_succeeded",
                event_context={
                    "project": new_project,
                },
            )
