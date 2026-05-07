import os
from unittest import mock

from django.test import TestCase, override_settings

from waldur_openportal import config


class OpenPortalConfigurationTest(TestCase):
    def setUp(self):
        self.env_patcher = mock.patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_is_config_available_returns_false_when_plugin_disabled(self):
        with override_settings(WALDUR_OPENPORTAL={"ENABLED": False}):
            result = config.is_config_available()
            self.assertFalse(result)

    def test_is_config_available_returns_false_when_env_var_missing(self):
        with override_settings(WALDUR_OPENPORTAL={"ENABLED": True}):
            result = config.is_config_available()
            self.assertFalse(result)

    def test_is_config_available_returns_false_when_env_var_empty(self):
        with override_settings(WALDUR_OPENPORTAL={"ENABLED": True}):
            os.environ["OPENPORTAL_CONFIG"] = ""
            result = config.is_config_available()
            self.assertFalse(result)

    def test_is_config_available_returns_true_when_enabled_and_config_set(self):
        with override_settings(WALDUR_OPENPORTAL={"ENABLED": True}):
            os.environ["OPENPORTAL_CONFIG"] = "/path/to/config.json"
            result = config.is_config_available()
            self.assertTrue(result)

    def test_is_config_available_uses_default_enabled_false(self):
        with override_settings(WALDUR_OPENPORTAL={}):
            os.environ["OPENPORTAL_CONFIG"] = "/path/to/config.json"
            result = config.is_config_available()
            self.assertFalse(result)


class OpenPortalEnsureConfigLoadedTest(TestCase):
    def setUp(self):
        self.env_patcher = mock.patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_returns_false_when_plugin_disabled(self):
        with override_settings(WALDUR_OPENPORTAL={"ENABLED": False}):
            result = config.ensure_config_loaded()
            self.assertFalse(result)

    def test_returns_false_when_config_file_not_set(self):
        with override_settings(WALDUR_OPENPORTAL={"ENABLED": True}):
            result = config.ensure_config_loaded()
            self.assertFalse(result)

    @mock.patch("waldur_openportal.config.is_config_loaded")
    def test_returns_true_when_already_loaded(self, mock_is_loaded):
        mock_is_loaded.return_value = True
        with override_settings(WALDUR_OPENPORTAL={"ENABLED": True}):
            os.environ["OPENPORTAL_CONFIG"] = "/path/to/config.json"
            result = config.ensure_config_loaded()
            self.assertTrue(result)

    @mock.patch("waldur_openportal.config.load_config")
    @mock.patch("waldur_openportal.config.is_config_loaded")
    def test_loads_config_and_returns_true_on_success(self, mock_is_loaded, mock_load):
        mock_is_loaded.return_value = False
        mock_load.return_value = None  # Successful load

        with override_settings(WALDUR_OPENPORTAL={"ENABLED": True}):
            os.environ["OPENPORTAL_CONFIG"] = "/path/to/config.json"
            result = config.ensure_config_loaded()

        self.assertTrue(result)
        mock_load.assert_called_once_with("/path/to/config.json")

    @mock.patch("waldur_openportal.config.load_config")
    @mock.patch("waldur_openportal.config.is_config_loaded")
    def test_returns_false_on_load_exception(self, mock_is_loaded, mock_load):
        mock_is_loaded.return_value = False
        mock_load.side_effect = Exception("Config load failed")

        with override_settings(WALDUR_OPENPORTAL={"ENABLED": True}):
            os.environ["OPENPORTAL_CONFIG"] = "/path/to/config.json"
            result = config.ensure_config_loaded()

        self.assertFalse(result)
        mock_load.assert_called_once_with("/path/to/config.json")

    @mock.patch("waldur_openportal.config.logging.getLogger")
    def test_logs_debug_message_when_disabled(self, mock_get_logger):
        mock_logger = mock.Mock()
        mock_get_logger.return_value = mock_logger

        with override_settings(WALDUR_OPENPORTAL={"ENABLED": False}):
            config.ensure_config_loaded()

        mock_logger.debug.assert_called_once_with(
            "OpenPortal plugin is disabled, skipping config loading"
        )

    @mock.patch("waldur_openportal.config.logging.getLogger")
    def test_logs_warning_when_config_not_set(self, mock_get_logger):
        mock_logger = mock.Mock()
        mock_get_logger.return_value = mock_logger

        with override_settings(WALDUR_OPENPORTAL={"ENABLED": True}):
            config.ensure_config_loaded()

        mock_logger.warning.assert_called_once_with(
            "OPENPORTAL_CONFIG environment variable not set, skipping OpenPortal operations"
        )

    @mock.patch("waldur_openportal.config.load_config")
    @mock.patch("waldur_openportal.config.is_config_loaded")
    @mock.patch("waldur_openportal.config.logging.getLogger")
    def test_logs_debug_on_successful_load(
        self, mock_get_logger, mock_is_loaded, mock_load
    ):
        mock_logger = mock.Mock()
        mock_get_logger.return_value = mock_logger
        mock_is_loaded.return_value = False
        mock_load.return_value = None

        with override_settings(WALDUR_OPENPORTAL={"ENABLED": True}):
            os.environ["OPENPORTAL_CONFIG"] = "/test/config.json"
            config.ensure_config_loaded()

        mock_logger.debug.assert_called_once_with(
            "OpenPortal config loaded from /test/config.json"
        )

    @mock.patch("waldur_openportal.config.load_config")
    @mock.patch("waldur_openportal.config.is_config_loaded")
    @mock.patch("waldur_openportal.config.logging.getLogger")
    def test_logs_error_on_load_failure(
        self, mock_get_logger, mock_is_loaded, mock_load
    ):
        mock_logger = mock.Mock()
        mock_get_logger.return_value = mock_logger
        mock_is_loaded.return_value = False
        mock_load.side_effect = Exception("Load error")

        with override_settings(WALDUR_OPENPORTAL={"ENABLED": True}):
            os.environ["OPENPORTAL_CONFIG"] = "/test/config.json"
            config.ensure_config_loaded()

        mock_logger.error.assert_called_once_with(
            "Failed to load OpenPortal config from '/test/config.json': Load error"
        )
