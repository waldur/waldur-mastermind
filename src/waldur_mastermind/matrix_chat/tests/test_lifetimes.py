import json
from datetime import timedelta
from unittest import mock

import httpx
import respx
from constance.test import override_config
from django.test import TestCase
from django.utils import timezone
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import extension, matrix_client, models, tasks

HOMESERVER = "https://matrix.example.com"


@override_config(
    MATRIX_HOMESERVER_URL=HOMESERVER,
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
class LogoutDeviceHelperLoginTest(TestCase):
    @respx.mock
    def test_helper_login_expires_even_if_logout_fails(self):
        # The helper login exists only to log the device out; with a refresh
        # token it expires on the homeserver's access_token_ttl, so a failed
        # /logout no longer leaves a token that never expires.
        login = respx.post(f"{HOMESERVER}/_matrix/client/v3/login").mock(
            return_value=httpx.Response(200, json={"access_token": "helper"})
        )
        respx.post(f"{HOMESERVER}/_matrix/client/v3/logout").mock(
            return_value=httpx.Response(503, json={})
        )

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.logout_device("@alice:matrix.example.com", "OLD_DEVICE")

        self.assertTrue(json.loads(login.calls.last.request.content)["refresh_token"])


@override_config(
    MATRIX_HOMESERVER_URL=HOMESERVER,
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
class ProbeWebTokenLifetimeTest(TestCase):
    def _login(self):
        return respx.post(f"{HOMESERVER}/_matrix/client/v3/login").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "probe",
                    "refresh_token": "probe-refresh",
                    "expires_in_ms": 300000,
                },
            )
        )

    @respx.mock
    def test_returns_the_expiry_and_signs_the_probe_out(self):
        login = self._login()
        logout = respx.post(f"{HOMESERVER}/_matrix/client/v3/logout").mock(
            return_value=httpx.Response(200, json={})
        )

        lifetime = matrix_client.probe_web_token_lifetime("@staff:matrix.example.com")

        self.assertEqual(lifetime, 300000)
        self.assertEqual(
            logout.calls.last.request.headers["Authorization"], "Bearer probe"
        )
        # A web device, so pruning and deactivation remove it should the
        # logout fail.
        device_id = json.loads(login.calls.last.request.content)["device_id"]
        self.assertTrue(device_id.startswith(matrix_client.WEB_DEVICE_PREFIX))

    @respx.mock
    def test_a_rejected_logout_keeps_the_measurement(self):
        self._login()
        respx.post(f"{HOMESERVER}/_matrix/client/v3/logout").mock(
            return_value=httpx.Response(503, json={})
        )

        with self.assertLogs(
            "waldur_mastermind.matrix_chat.matrix_client", "WARNING"
        ) as logs:
            lifetime = matrix_client.probe_web_token_lifetime(
                "@staff:matrix.example.com"
            )

        self.assertEqual(lifetime, 300000)
        self.assertIn("503", logs.output[0])

    @respx.mock
    def test_an_unreachable_logout_keeps_the_measurement(self):
        self._login()
        respx.post(f"{HOMESERVER}/_matrix/client/v3/logout").mock(
            side_effect=httpx.ConnectError("down")
        )

        with self.assertLogs("waldur_mastermind.matrix_chat.matrix_client", "WARNING"):
            lifetime = matrix_client.probe_web_token_lifetime(
                "@staff:matrix.example.com"
            )

        self.assertEqual(lifetime, 300000)


@override_config(
    MATRIX_HOMESERVER_URL=HOMESERVER,
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
class HomeserverReachableTest(TestCase):
    @respx.mock
    def test_answers_when_versions_responds(self):
        respx.get(f"{HOMESERVER}/_matrix/client/versions").mock(
            return_value=httpx.Response(200, json={"versions": ["v1.11"]})
        )

        self.assertTrue(matrix_client.is_homeserver_reachable())

    @respx.mock
    def test_does_not_answer_with_an_error(self):
        respx.get(f"{HOMESERVER}/_matrix/client/versions").mock(
            return_value=httpx.Response(502)
        )

        self.assertFalse(matrix_client.is_homeserver_reachable())

    @respx.mock
    def test_does_not_answer_when_unreachable(self):
        respx.get(f"{HOMESERVER}/_matrix/client/versions").mock(
            side_effect=httpx.ConnectError("down")
        )

        self.assertFalse(matrix_client.is_homeserver_reachable())


@mock.patch("waldur_mastermind.matrix_chat.tasks.prune_web_devices")
@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class PruneAllWebDevicesTest(TestCase):
    def test_prunes_only_users_who_may_have_web_devices(self, mock_client, mock_prune):
        # Pruning on session start never reaches users who do not come back,
        # and asking the homeserver about users without web devices is waste.
        mock_client.is_homeserver_configured.return_value = True
        mock_client.is_homeserver_reachable.return_value = True
        for name, provisioned, marked in (
            ("alice", True, True),
            ("bob", True, True),
            ("idle", True, False),
            ("pending", False, True),
        ):
            models.MatrixUserProfile.objects.create(
                user=structure_factories.UserFactory(username=name),
                matrix_user_id=f"@{name}:matrix.example.com",
                provisioned=provisioned,
                last_web_session_at=timezone.now() if marked else None,
            )

        tasks.prune_all_web_devices()

        self.assertEqual(
            sorted(c.args[0] for c in mock_prune.delay.call_args_list),
            ["@alice:matrix.example.com", "@bob:matrix.example.com"],
        )

    def test_skips_without_a_homeserver(self, mock_client, mock_prune):
        mock_client.is_homeserver_configured.return_value = False
        models.MatrixUserProfile.objects.create(
            user=structure_factories.UserFactory(),
            matrix_user_id="@alice:matrix.example.com",
            provisioned=True,
        )

        tasks.prune_all_web_devices()

        mock_prune.delay.assert_not_called()

    def test_skips_while_the_homeserver_does_not_answer(self, mock_client, mock_prune):
        # Each user's task would only fail on its own; tomorrow's run catches up.
        mock_client.is_homeserver_configured.return_value = True
        mock_client.is_homeserver_reachable.return_value = False
        models.MatrixUserProfile.objects.create(
            user=structure_factories.UserFactory(),
            matrix_user_id="@alice:matrix.example.com",
            provisioned=True,
        )

        with self.assertLogs("waldur_mastermind.matrix_chat.tasks", "WARNING"):
            tasks.prune_all_web_devices()

        mock_prune.delay.assert_not_called()

    def test_runs_daily(self, mock_client, mock_prune):
        tasks_by_name = {
            entry["task"]
            for entry in extension.MatrixChatExtension.celery_tasks().values()
        }
        self.assertIn(
            "waldur_mastermind.matrix_chat.prune_all_web_devices", tasks_by_name
        )


@override_config(
    MATRIX_HOMESERVER_URL=HOMESERVER,
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
class WebDeviceTrackingTest(TestCase):
    user_id = "@alice:matrix.example.com"

    def setUp(self):
        self.profile = models.MatrixUserProfile.objects.create(
            user=structure_factories.UserFactory(),
            matrix_user_id=self.user_id,
            provisioned=True,
        )

    def _mock_login(self):
        respx.post(f"{HOMESERVER}/_matrix/client/v3/login").mock(
            return_value=httpx.Response(
                200, json={"access_token": "token", "expires_in_ms": 300000}
            )
        )

    def _mark(self):
        self.profile.last_web_session_at = timezone.now() - timedelta(days=2)
        self.profile.save(update_fields=["last_web_session_at"])
        return self.profile.last_web_session_at

    def _prune(self, devices, logout=None, keep_device_id=None):
        with (
            mock.patch.object(matrix_client, "list_devices", return_value=devices),
            mock.patch.object(matrix_client, "logout_device", side_effect=logout),
        ):
            tasks.prune_web_devices(self.user_id, keep_device_id)
        self.profile.refresh_from_db()

    @respx.mock
    def test_a_web_session_marks_the_user(self):
        self._mock_login()

        matrix_client.create_web_session(self.user_id)

        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.last_web_session_at)

    @respx.mock
    def test_the_diagnostics_probe_marks_the_user(self):
        # Its device is left to pruning if the logout fails.
        self._mock_login()
        respx.post(f"{HOMESERVER}/_matrix/client/v3/logout").mock(
            return_value=httpx.Response(200, json={})
        )

        matrix_client.probe_web_token_lifetime(self.user_id)

        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.last_web_session_at)

    def test_unmarks_the_user_once_no_web_device_is_left(self):
        self._mark()
        idle = {"device_id": "WALDUR_WEB_OLD", "last_seen_ts": 0}
        element = {"device_id": "ELEMENT", "last_seen_ts": 0}

        self._prune([idle, element])

        self.assertIsNone(self.profile.last_web_session_at)

    def test_keeps_the_mark_while_a_web_device_is_in_use(self):
        marked_at = self._mark()
        now_ms = int(timezone.now().timestamp() * 1000)

        self._prune([{"device_id": "WALDUR_WEB_OPEN", "last_seen_ts": now_ms}])

        self.assertEqual(self.profile.last_web_session_at, marked_at)

    def test_keeps_the_mark_when_a_logout_fails(self):
        marked_at = self._mark()

        self._prune(
            [{"device_id": "WALDUR_WEB_OLD", "last_seen_ts": 0}],
            logout=matrix_client.MatrixClientError("down"),
        )

        self.assertEqual(self.profile.last_web_session_at, marked_at)

    def test_keeps_the_mark_of_the_session_that_started_the_prune(self):
        # The homeserver may list devices before it shows the new one.
        marked_at = self._mark()

        self._prune([], keep_device_id="WALDUR_WEB_NEW")

        self.assertEqual(self.profile.last_web_session_at, marked_at)

    def test_keeps_the_mark_of_a_session_started_during_the_prune(self):
        self._mark()
        newer = timezone.now()

        def list_devices(matrix_user_id):
            # A session starts after the prune read the mark.
            models.MatrixUserProfile.objects.filter(pk=self.profile.pk).update(
                last_web_session_at=newer
            )
            return []

        with mock.patch.object(matrix_client, "list_devices", side_effect=list_devices):
            tasks.prune_web_devices(self.user_id)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.last_web_session_at, newer)


@mock.patch(
    "waldur_mastermind.matrix_chat.views.matrix_client.probe_web_token_lifetime"
)
@mock.patch("waldur_mastermind.matrix_chat.views.httpx.get")
class WebTokenLifetimeDiagnosticsTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    def _check(self):
        response = self.client.get("/api/admin/matrix/diagnostics/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {c["name"]: c for c in response.data["checks"]}["web_token_lifetime"]

    def _provision_staff(self):
        models.MatrixUserProfile.objects.create(
            user=self.staff,
            matrix_user_id="@staff:matrix.example.com",
            provisioned=True,
        )

    def test_flags_tokens_that_never_expire(self, mock_get, mock_probe):
        self._provision_staff()
        mock_probe.return_value = None

        check = self._check()

        self.assertFalse(check["ok"])
        self.assertIn("access_token_ttl", check["detail"])

    def test_flags_tokens_that_live_for_days(self, mock_get, mock_probe):
        self._provision_staff()
        mock_probe.return_value = 7 * 24 * 3600 * 1000

        check = self._check()

        self.assertFalse(check["ok"])
        self.assertIn("access_token_ttl", check["detail"])

    def test_accepts_short_lived_tokens(self, mock_get, mock_probe):
        self._provision_staff()
        mock_probe.return_value = 300000

        check = self._check()

        self.assertTrue(check["ok"])
        self.assertIn("300", check["detail"])

    def test_is_skipped_for_staff_without_a_matrix_account(self, mock_get, mock_probe):
        check = self._check()

        # Nothing was measured, so nothing failed: a skip must not fail the
        # diagnostics as a whole.
        self.assertTrue(check["ok"])
        self.assertTrue(check["detail"].startswith("Skipped"))
        mock_probe.assert_not_called()
