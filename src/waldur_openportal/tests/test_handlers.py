from unittest import mock

from django.test import TestCase, override_settings

from waldur_openportal import handlers


class IfPluginEnabledDecoratorTest(TestCase):
    """Tests for the if_plugin_enabled decorator"""

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    @mock.patch("waldur_openportal.handlers.logger")
    def test_handler_executes_when_plugin_enabled(self, mock_logger):
        """Test that handlers execute when plugin is enabled"""

        @handlers.if_plugin_enabled
        def test_handler(*args, **kwargs):
            return "handler_executed"

        result = test_handler()

        self.assertEqual(result, "handler_executed")
        mock_logger.debug.assert_not_called()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    @mock.patch("waldur_openportal.handlers.logger")
    def test_handler_skipped_when_plugin_disabled(self, mock_logger):
        """Test that handlers are skipped when plugin is disabled"""

        @handlers.if_plugin_enabled
        def test_handler(*args, **kwargs):
            return "handler_executed"

        result = test_handler()

        self.assertIsNone(result)
        mock_logger.debug.assert_called_once_with(
            "Skipping OpenPortal handler because plugin is disabled."
        )

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    def test_decorated_handlers_not_executed_when_disabled(self):
        """Test that actual handlers like schedule_sync are not executed when disabled"""

        with mock.patch("waldur_openportal.handlers.tasks") as mock_tasks:
            result = handlers.schedule_sync()

            self.assertIsNone(result)
            mock_tasks.sync.delay.assert_not_called()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    @mock.patch("django.db.transaction.get_connection")
    @mock.patch("django.db.transaction.on_commit")
    def test_decorated_handlers_execute_when_enabled(
        self, mock_on_commit, mock_get_connection
    ):
        """Test that actual handlers like schedule_sync execute when enabled"""

        mock_connection = mock.Mock()
        mock_connection.in_atomic_block = False
        mock_get_connection.return_value = mock_connection

        with mock.patch("waldur_openportal.handlers.tasks"):
            handlers.schedule_sync()

            mock_on_commit.assert_called_once()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    def test_role_granted_handler_not_executed_when_disabled(self):
        """Test that role_granted handler is not executed when disabled"""

        mock_instance = mock.Mock()
        mock_instance.role = mock.Mock()
        mock_instance.user = mock.Mock()

        with mock.patch("waldur_openportal.handlers.update_user") as mock_update_user:
            result = handlers.role_granted(None, mock_instance)

            self.assertIsNone(result)
            mock_update_user.assert_not_called()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    def test_update_allocation_credits_not_executed_when_disabled(self):
        """Test that update_allocation_credits handler is not executed when disabled"""

        mock_resource = mock.Mock()

        with mock.patch("waldur_openportal.handlers.logger") as mock_logger:
            result = handlers.update_allocation_credits(None, mock_resource)

            self.assertIsNone(result)
            # Should not reach the logging inside the function
            mock_logger.info.assert_not_called()
