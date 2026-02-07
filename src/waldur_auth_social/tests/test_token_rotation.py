from unittest import mock

import responses
from constance import config
from constance.test.unittest import override_config
from django.core.cache import cache
from django.test import TestCase, override_settings

from waldur_auth_social import tasks, utils

EDUTEAMS_SETTINGS = {
    "REMOTE_EDUTEAMS_REFRESH_TOKEN": "settings_refresh_token",
    "REMOTE_EDUTEAMS_USERINFO_URL": "https://proxy.acc.researcher-access.org/api/userinfo",
    "REMOTE_EDUTEAMS_TOKEN_URL": "https://proxy.acc.researcher-access.org/OIDC/token",
    "REMOTE_EDUTEAMS_CLIENT_ID": "WaldurId",
    "REMOTE_EDUTEAMS_SECRET": "WaldurSecret",
    "REMOTE_EDUTEAMS_ENABLED": True,
}

TOKEN_URL = "https://proxy.acc.researcher-access.org/OIDC/token"


@override_settings(WALDUR_AUTH_SOCIAL=EDUTEAMS_SETTINGS)
class GetCurrentRefreshTokenTest(TestCase):
    @override_config(REMOTE_EDUTEAMS_REFRESH_TOKEN="constance_token")
    def test_constance_token_takes_precedence(self):
        self.assertEqual(utils._get_current_refresh_token(), "constance_token")

    @override_config(REMOTE_EDUTEAMS_REFRESH_TOKEN="")
    def test_falls_back_to_settings_when_constance_is_empty(self):
        self.assertEqual(utils._get_current_refresh_token(), "settings_refresh_token")


@override_settings(
    WALDUR_AUTH_SOCIAL={
        **EDUTEAMS_SETTINGS,
        "REMOTE_EDUTEAMS_REFRESH_TOKEN": "",
    }
)
class NoRefreshTokenTest(TestCase):
    @override_config(REMOTE_EDUTEAMS_REFRESH_TOKEN="")
    def test_raises_error_when_both_are_empty(self):
        cache.delete("REMOTE_EDUTEAMS_ACCESS_TOKEN")
        with self.assertRaises(RuntimeError):
            utils.refresh_remote_eduteams_token()


@override_settings(WALDUR_AUTH_SOCIAL=EDUTEAMS_SETTINGS)
class RefreshTokenRotationTest(TestCase):
    def setUp(self):
        cache.delete("REMOTE_EDUTEAMS_ACCESS_TOKEN")

    @responses.activate
    @override_config(REMOTE_EDUTEAMS_REFRESH_TOKEN="")
    def test_new_refresh_token_saved_to_constance(self):
        responses.add(
            method="POST",
            url=TOKEN_URL,
            json={
                "access_token": "new_access",
                "refresh_token": "rotated_refresh_token",
            },
        )
        utils.refresh_remote_eduteams_token()

        self.assertEqual(config.REMOTE_EDUTEAMS_REFRESH_TOKEN, "rotated_refresh_token")

    @responses.activate
    @override_config(REMOTE_EDUTEAMS_REFRESH_TOKEN="existing_constance_token")
    def test_missing_refresh_token_in_response_does_not_overwrite(self):
        responses.add(
            method="POST",
            url=TOKEN_URL,
            json={"access_token": "new_access"},
        )
        utils.refresh_remote_eduteams_token()

        self.assertEqual(
            config.REMOTE_EDUTEAMS_REFRESH_TOKEN, "existing_constance_token"
        )

    @responses.activate
    @override_config(REMOTE_EDUTEAMS_REFRESH_TOKEN="")
    def test_scope_includes_offline_access(self):
        responses.add(
            method="POST",
            url=TOKEN_URL,
            json={
                "access_token": "new_access",
                "refresh_token": "rotated_refresh_token",
            },
        )
        utils.refresh_remote_eduteams_token()

        self.assertEqual(len(responses.calls), 1)
        request_body = responses.calls[0].request.body
        self.assertIn("offline_access", request_body)

    @responses.activate
    @override_config(REMOTE_EDUTEAMS_REFRESH_TOKEN="")
    def test_cached_token_is_returned_without_request(self):
        cache.set("REMOTE_EDUTEAMS_ACCESS_TOKEN", "cached_token", 30 * 60)
        result = utils.refresh_remote_eduteams_token()
        self.assertEqual(result, "cached_token")
        self.assertEqual(len(responses.calls), 0)

    @responses.activate
    @override_config(REMOTE_EDUTEAMS_REFRESH_TOKEN="")
    def test_force_bypasses_cache(self):
        cache.set("REMOTE_EDUTEAMS_ACCESS_TOKEN", "cached_token", 30 * 60)
        responses.add(
            method="POST",
            url=TOKEN_URL,
            json={
                "access_token": "fresh_access",
                "refresh_token": "fresh_refresh",
            },
        )
        result = utils.refresh_remote_eduteams_token(force=True)
        self.assertEqual(result, "fresh_access")
        self.assertEqual(len(responses.calls), 1)


@override_settings(
    WALDUR_AUTH_SOCIAL={**EDUTEAMS_SETTINGS, "REMOTE_EDUTEAMS_ENABLED": False}
)
class RotationTaskDisabledTest(TestCase):
    @mock.patch("waldur_auth_social.utils.refresh_remote_eduteams_token")
    def test_task_skipped_when_disabled(self, mock_refresh):
        tasks.rotate_remote_eduteams_token()
        mock_refresh.assert_not_called()


@override_settings(WALDUR_AUTH_SOCIAL=EDUTEAMS_SETTINGS)
class RotationTaskTest(TestCase):
    @mock.patch("waldur_auth_social.utils.refresh_remote_eduteams_token")
    def test_task_calls_refresh_with_force(self, mock_refresh):
        tasks.rotate_remote_eduteams_token()
        mock_refresh.assert_called_once_with(force=True)

    @mock.patch(
        "waldur_auth_social.utils.refresh_remote_eduteams_token",
        side_effect=Exception("token endpoint down"),
    )
    def test_task_catches_exceptions(self, mock_refresh):
        # Should not raise
        tasks.rotate_remote_eduteams_token()
        mock_refresh.assert_called_once()
