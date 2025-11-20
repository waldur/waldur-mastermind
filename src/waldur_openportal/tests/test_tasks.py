from unittest import mock

from django.test import TestCase, override_settings

from waldur_openportal import tasks


class TaskConfigurationTest(TestCase):
    @mock.patch("waldur_openportal.tasks.openportal.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.openportal.get_portal")
    @mock.patch("waldur_openportal.tasks.openportal.sync_offerings")
    @mock.patch("waldur_openportal.tasks.models.ProjectTemplate.objects.all")
    def test_sync_offering_agents_skips_when_config_unavailable(
        self, mock_templates, mock_sync, mock_portal, mock_config
    ):
        mock_config.return_value = False

        tasks.sync_offering_agents()

        mock_config.assert_called_once()
        mock_templates.assert_not_called()
        mock_portal.assert_not_called()
        mock_sync.assert_not_called()

    @mock.patch("waldur_openportal.tasks.openportal.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.openportal.get_portal")
    @mock.patch("waldur_openportal.tasks.openportal.sync_offerings")
    @mock.patch("waldur_openportal.tasks.models.ProjectTemplate.objects.all")
    def test_sync_offering_agents_proceeds_when_config_available(
        self, mock_templates, mock_sync, mock_portal, mock_config
    ):
        mock_config.return_value = True
        mock_portal.return_value = "test-portal"
        mock_templates.return_value = []

        tasks.sync_offering_agents()

        mock_config.assert_called_once()
        mock_templates.assert_called_once()
        mock_portal.assert_called_once()
        mock_sync.assert_called_once_with([])

    @mock.patch("waldur_openportal.tasks.openportal.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.openportal.fetch_jobs")
    def test_sync_board_skips_when_config_unavailable(self, mock_fetch, mock_config):
        mock_config.return_value = False

        tasks.sync_board()

        mock_config.assert_called_once()
        mock_fetch.assert_not_called()

    @mock.patch("waldur_openportal.tasks.openportal.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.openportal.fetch_jobs")
    def test_sync_board_proceeds_when_config_available(self, mock_fetch, mock_config):
        mock_config.return_value = True
        mock_fetch.return_value = []

        tasks.sync_board()

        mock_config.assert_called_once()
        mock_fetch.assert_called_once()

    @mock.patch("waldur_openportal.tasks.logger")
    @mock.patch("waldur_openportal.tasks.openportal.ensure_config_loaded")
    def test_sync_offering_agents_logs_appropriate_message(
        self, mock_config, mock_logger
    ):
        mock_config.return_value = False

        tasks.sync_offering_agents()

        mock_logger.info.assert_called_once_with(
            "OpenPortal not enabled or config not available, skipping sync_offering_agents"
        )

    @mock.patch("waldur_openportal.tasks.logger")
    @mock.patch("waldur_openportal.tasks.openportal.ensure_config_loaded")
    def test_sync_board_logs_appropriate_message(self, mock_config, mock_logger):
        mock_config.return_value = False

        tasks.sync_board()

        mock_logger.info.assert_called_once_with(
            "OpenPortal not enabled or config not available, skipping sync_board"
        )


class TaskIntegrationTest(TestCase):
    """Integration tests that verify the tasks work correctly with real settings"""

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    @mock.patch("waldur_openportal.tasks.models.ProjectTemplate.objects.all")
    def test_sync_offering_agents_with_disabled_setting(self, mock_templates):
        """Test that sync_offering_agents respects ENABLED=False setting"""
        tasks.sync_offering_agents()
        mock_templates.assert_not_called()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    @mock.patch("waldur_openportal.tasks.openportal.fetch_jobs")
    def test_sync_board_with_disabled_setting(self, mock_fetch):
        """Test that sync_board respects ENABLED=False setting"""
        tasks.sync_board()
        mock_fetch.assert_not_called()
