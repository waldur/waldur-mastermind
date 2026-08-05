"""
Integration tests demonstrating the OpenPortal configuration improvements.

These tests show how the modernized configuration system works end-to-end,
replacing the obsolete have_openportal() checks with proper ENABLED setting validation.
"""

import os
from unittest import mock

from django.test import TestCase, override_settings

from waldur_openportal import config, exceptions, tasks
from waldur_openportal.board import OpenPortalBoard
from waldur_openportal.client import OpenPortalRunner


class OpenPortalIntegrationTest(TestCase):
    """Test the complete OpenPortal configuration system end-to-end"""

    def setUp(self):
        self.env_patcher = mock.patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    def test_disabled_plugin_blocks_all_operations(self):
        """Test that when ENABLED=False, all OpenPortal operations are blocked"""
        # Configuration functions return False
        self.assertFalse(config.is_config_available())
        self.assertFalse(config.ensure_config_loaded())

        # Client and board raise appropriate errors
        with self.assertRaises(exceptions.OpenPortalError) as cm:
            OpenPortalRunner()
        self.assertIn("not enabled", str(cm.exception))

        with self.assertRaises(exceptions.OpenPortalError) as cm:
            OpenPortalBoard()
        self.assertIn("not enabled", str(cm.exception))

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    def test_enabled_plugin_without_config_fails_gracefully(self):
        """Test that when ENABLED=True but no config file, operations fail gracefully"""
        # Configuration functions detect missing config
        self.assertFalse(config.is_config_available())
        self.assertFalse(config.ensure_config_loaded())

        # Client and board fail with clear messages
        with self.assertRaises(exceptions.OpenPortalError) as cm:
            OpenPortalRunner()
        self.assertIn("not available", str(cm.exception))

        with self.assertRaises(exceptions.OpenPortalError) as cm:
            OpenPortalBoard()
        self.assertIn("not available", str(cm.exception))

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    @mock.patch("waldur_openportal.config.load_config")
    @mock.patch("waldur_openportal.config.is_config_loaded")
    def test_enabled_plugin_with_config_succeeds(self, mock_is_loaded, mock_load):
        """Test that when ENABLED=True and config available, operations succeed"""
        mock_is_loaded.return_value = False  # Need to load
        mock_load.return_value = None  # Successful load

        os.environ["OPENPORTAL_CONFIG"] = "/path/to/config.json"

        # Configuration functions work
        self.assertTrue(config.is_config_available())
        self.assertTrue(config.ensure_config_loaded())

        # Client and board can be created
        runner = OpenPortalRunner()
        self.assertIsInstance(runner, OpenPortalRunner)

        board = OpenPortalBoard()
        self.assertIsInstance(board, OpenPortalBoard)

        # Config loading was called
        mock_load.assert_called_with("/path/to/config.json")

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    @mock.patch("waldur_openportal.config.is_config_loaded")
    def test_already_loaded_config_skips_reload(self, mock_is_loaded):
        """Test that already loaded config doesn't trigger reload"""
        mock_is_loaded.return_value = True  # Already loaded

        os.environ["OPENPORTAL_CONFIG"] = "/path/to/config.json"

        self.assertTrue(config.is_config_available())
        self.assertTrue(config.ensure_config_loaded())

        # No load_config should be called since already loaded
        with mock.patch("waldur_openportal.config.load_config") as mock_load:
            config.ensure_config_loaded()
            mock_load.assert_not_called()


class TaskIntegrationTest(TestCase):
    """Test that tasks properly respect the ENABLED setting"""

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    @mock.patch("waldur_openportal.tasks.openportal.sync_offerings")
    @mock.patch("waldur_openportal.tasks.models.ProjectTemplate.objects.all")
    def test_tasks_skip_when_disabled(self, mock_templates, mock_sync):
        """Test that tasks skip execution when OpenPortal is disabled"""
        # Tasks should skip without calling OpenPortal functions
        tasks.sync_offering_agents()
        tasks.sync_board()

        # OpenPortal functions should not be called
        mock_templates.assert_not_called()
        mock_sync.assert_not_called()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    @mock.patch("waldur_openportal.tasks.openportal.sync_offerings")
    @mock.patch("waldur_openportal.tasks.openportal.get_portal")
    @mock.patch("waldur_openportal.tasks.models.ProjectTemplate.objects.all")
    @mock.patch("waldur_openportal.tasks.config.is_config_loaded")
    @mock.patch("waldur_openportal.tasks.config.load_config")
    def test_tasks_proceed_when_enabled_with_config(
        self, mock_load, mock_is_loaded, mock_templates, mock_portal, mock_sync
    ):
        """Test that tasks proceed when OpenPortal is enabled and configured"""
        mock_is_loaded.return_value = False
        mock_templates.return_value = []
        mock_portal.return_value = "test-portal"

        # Set config file
        with mock.patch.dict(os.environ, {"OPENPORTAL_CONFIG": "/test/config.json"}):
            tasks.sync_offering_agents()

        # OpenPortal functions should be called
        mock_load.assert_called_with("/test/config.json")
        mock_templates.assert_called_once()
        mock_portal.assert_called_once()
        mock_sync.assert_called_once_with([])


class ErrorHandlingTest(TestCase):
    """Test error handling improvements"""

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    @mock.patch("waldur_openportal.config.load_config")
    @mock.patch("waldur_openportal.config.is_config_loaded")
    def test_config_load_failure_handled_gracefully(self, mock_is_loaded, mock_load):
        """Test that config load failures are handled gracefully"""
        mock_is_loaded.return_value = False
        mock_load.side_effect = Exception("Config file corrupted")

        os.environ["OPENPORTAL_CONFIG"] = "/bad/config.json"

        # Should not crash, just return False
        self.assertFalse(config.ensure_config_loaded())

        # Operations should fail with clear messages
        with self.assertRaises(exceptions.OpenPortalError) as cm:
            OpenPortalRunner()
        self.assertIn("not available", str(cm.exception))

    def test_missing_enabled_setting_defaults_to_false(self):
        """Test that missing ENABLED setting defaults to False"""
        with override_settings(WALDUR_OPENPORTAL={}):
            self.assertFalse(config.is_config_available())
            self.assertFalse(config.ensure_config_loaded())

        # Should behave as disabled
        with override_settings(WALDUR_OPENPORTAL={}):
            with self.assertRaises(exceptions.OpenPortalError):
                OpenPortalRunner()
