from unittest import mock

from django.test import TestCase, override_settings

from waldur_openportal import exceptions
from waldur_openportal.board import OpenPortalBoard
from waldur_openportal.client import OpenPortalRunner


class OpenPortalRunnerTest(TestCase):
    @mock.patch("waldur_openportal.config.ensure_config_loaded")
    def test_init_raises_error_when_config_unavailable(self, mock_config):
        mock_config.return_value = False

        with self.assertRaises(exceptions.OpenPortalError) as cm:
            OpenPortalRunner()

        self.assertEqual(
            str(cm.exception),
            "OpenPortal is not enabled or configuration is not available",
        )
        mock_config.assert_called_once()

    @mock.patch("waldur_openportal.config.ensure_config_loaded")
    def test_init_succeeds_when_config_available(self, mock_config):
        mock_config.return_value = True

        runner = OpenPortalRunner()

        self.assertIsInstance(runner, OpenPortalRunner)
        mock_config.assert_called_once()

    @mock.patch("waldur_openportal.config.is_config_available")
    def test_health_raises_error_when_config_unavailable(self, mock_config_available):
        mock_config_available.return_value = False

        with mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        ):
            runner = OpenPortalRunner()

        with self.assertRaises(exceptions.OpenPortalError) as cm:
            runner.health()

        self.assertEqual(
            str(cm.exception),
            "OpenPortal is not enabled or configuration is not available",
        )

    @mock.patch("waldur_openportal.config.is_config_available")
    @mock.patch("openportal.health")
    def test_health_succeeds_when_config_available(
        self, mock_health, mock_config_available
    ):
        mock_config_available.return_value = True
        mock_health_obj = mock.Mock()
        mock_health_obj.is_healthy.return_value = True
        mock_health.return_value = mock_health_obj

        with mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        ):
            runner = OpenPortalRunner()

        # health() method doesn't return anything, just validates
        runner.health()

        mock_health.assert_called_once()

    @mock.patch("waldur_openportal.config.is_config_available")
    def test_get_raises_error_when_config_unavailable(self, mock_config_available):
        mock_config_available.return_value = False

        with mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        ):
            runner = OpenPortalRunner()

        with self.assertRaises(exceptions.OpenPortalError) as cm:
            runner.get("test-uid")

        self.assertIn("cannot get job with UID 'test-uid'", str(cm.exception))

    @mock.patch("waldur_openportal.config.is_config_available")
    @mock.patch("openportal.get")
    def test_get_succeeds_when_config_available(self, mock_get, mock_config_available):
        mock_config_available.return_value = True
        mock_get.return_value = mock.Mock()

        with mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        ):
            runner = OpenPortalRunner()

        result = runner.get("test-uid")

        mock_get.assert_called_once_with("test-uid")
        self.assertIsNotNone(result)

    @mock.patch("waldur_openportal.config.is_config_available")
    def test_run_raises_error_when_config_unavailable(self, mock_config_available):
        mock_config_available.return_value = False

        with mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        ):
            runner = OpenPortalRunner()

        with self.assertRaises(exceptions.OpenPortalError) as cm:
            runner.run("test-command")

        self.assertIn("cannot run 'test-command'", str(cm.exception))

    @mock.patch("waldur_openportal.config.is_config_available")
    @mock.patch("openportal.run")
    def test_run_succeeds_when_config_available(self, mock_run, mock_config_available):
        mock_config_available.return_value = True
        mock_run.return_value = mock.Mock()

        with mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        ):
            runner = OpenPortalRunner()

        result = runner.run("test-command")

        mock_run.assert_called_once_with("test-command", 100)
        self.assertIsNotNone(result)


class OpenPortalBoardTest(TestCase):
    @mock.patch("waldur_openportal.config.ensure_config_loaded")
    def test_init_raises_error_when_config_unavailable(self, mock_config):
        mock_config.return_value = False

        with self.assertRaises(exceptions.OpenPortalError) as cm:
            OpenPortalBoard()

        self.assertEqual(
            str(cm.exception),
            "OpenPortal is not enabled or configuration is not available",
        )
        mock_config.assert_called_once()

    @mock.patch("waldur_openportal.config.ensure_config_loaded")
    def test_init_succeeds_when_config_available(self, mock_config):
        mock_config.return_value = True

        board = OpenPortalBoard()

        self.assertIsInstance(board, OpenPortalBoard)
        mock_config.assert_called_once()

    @mock.patch("waldur_openportal.config.is_config_available")
    def test_health_raises_error_when_config_unavailable(self, mock_config_available):
        mock_config_available.return_value = False

        with mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        ):
            board = OpenPortalBoard()

        with self.assertRaises(exceptions.OpenPortalError) as cm:
            board.health()

        self.assertEqual(
            str(cm.exception),
            "OpenPortal is not enabled or configuration is not available",
        )

    @mock.patch("waldur_openportal.config.is_config_available")
    @mock.patch("openportal.health")
    def test_health_succeeds_when_config_available(
        self, mock_health, mock_config_available
    ):
        mock_config_available.return_value = True
        mock_health_obj = mock.Mock()
        mock_health_obj.is_healthy.return_value = True
        mock_health.return_value = mock_health_obj

        with mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        ):
            board = OpenPortalBoard()

        # health() method doesn't return anything, just validates
        board.health()

        mock_health.assert_called_once()

    @mock.patch("waldur_openportal.config.is_config_available")
    def test_fetch_job_raises_error_when_config_unavailable(
        self, mock_config_available
    ):
        mock_config_available.return_value = False

        with mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        ):
            board = OpenPortalBoard()

        with self.assertRaises(exceptions.OpenPortalError) as cm:
            board.fetch_job("test-job-id")

        self.assertIn("cannot fetch job with ID 'test-job-id'", str(cm.exception))

    @mock.patch("waldur_openportal.config.is_config_available")
    @mock.patch("openportal.fetch_job")
    def test_fetch_job_succeeds_when_config_available(
        self, mock_fetch_job, mock_config_available
    ):
        mock_config_available.return_value = True
        mock_fetch_job.return_value = mock.Mock()

        with mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        ):
            board = OpenPortalBoard()

        result = board.fetch_job("test-job-id")

        mock_fetch_job.assert_called_once_with("test-job-id")
        self.assertIsNotNone(result)


class ClientBoardIntegrationTest(TestCase):
    """Integration tests that verify client and board work correctly with real settings"""

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    def test_runner_init_with_disabled_setting(self):
        """Test that OpenPortalRunner respects ENABLED=False setting"""
        with self.assertRaises(exceptions.OpenPortalError):
            OpenPortalRunner()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    def test_board_init_with_disabled_setting(self):
        """Test that OpenPortalBoard respects ENABLED=False setting"""
        with self.assertRaises(exceptions.OpenPortalError):
            OpenPortalBoard()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    @mock.patch("openportal.is_config_loaded")
    @mock.patch("openportal.load_config")
    def test_runner_init_with_enabled_setting_but_no_env_var(
        self, mock_load, mock_is_loaded
    ):
        """Test that OpenPortalRunner fails gracefully when ENABLED=True but no config file"""
        mock_is_loaded.return_value = False

        with self.assertRaises(exceptions.OpenPortalError):
            OpenPortalRunner()

        mock_load.assert_not_called()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    @mock.patch("openportal.is_config_loaded")
    @mock.patch("openportal.load_config")
    def test_board_init_with_enabled_setting_but_no_env_var(
        self, mock_load, mock_is_loaded
    ):
        """Test that OpenPortalBoard fails gracefully when ENABLED=True but no config file"""
        mock_is_loaded.return_value = False

        with self.assertRaises(exceptions.OpenPortalError):
            OpenPortalBoard()

        mock_load.assert_not_called()
