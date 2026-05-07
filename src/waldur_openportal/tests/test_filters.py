import os
import uuid
from unittest import mock

from django.test import TestCase, override_settings

from waldur_openportal.filters import _identifiers_for_project_uuid


class IdentifiersForProjectUuidTest(TestCase):
    def setUp(self):
        self.env_patcher = mock.patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()
        self.project_uuid = uuid.uuid4().hex

    def tearDown(self):
        self.env_patcher.stop()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    @mock.patch("openportal.get_portal")
    def test_does_not_call_get_portal_when_config_unavailable(self, mock_get_portal):
        identifiers = _identifiers_for_project_uuid(self.project_uuid)

        self.assertEqual(identifiers, set())
        mock_get_portal.assert_not_called()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    @mock.patch("openportal.get_portal", return_value="example.portal")
    @mock.patch("waldur_openportal.config.ensure_config_loaded", return_value=True)
    def test_calls_get_portal_when_config_loaded(
        self, mock_ensure_loaded, mock_get_portal
    ):
        identifiers = _identifiers_for_project_uuid(self.project_uuid)

        self.assertEqual(identifiers, set())
        mock_get_portal.assert_called_once()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    @mock.patch("openportal.get_portal")
    def test_does_not_call_get_portal_when_plugin_disabled(self, mock_get_portal):
        identifiers = _identifiers_for_project_uuid(self.project_uuid)

        self.assertEqual(identifiers, set())
        mock_get_portal.assert_not_called()
