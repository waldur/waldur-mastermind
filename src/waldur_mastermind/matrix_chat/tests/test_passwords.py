import json
from unittest import mock

import httpx
import respx
from constance.test import override_config
from django.test import TestCase
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event, Feed
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import matrix_client, models

HOMESERVER = "https://matrix.example.com"
REGISTER = f"{HOMESERVER}/_matrix/client/v3/register"
RESET = f"{HOMESERVER}/_synapse/admin/v1/reset_password/%40alice%3Amatrix.example.com"
ADMIN_USER = f"{HOMESERVER}/_synapse/admin/v2/users/%40alice%3Amatrix.example.com"
MATRIX = dict(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL=HOMESERVER,
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
    MATRIX_USER_REGISTRATION_SECRET="test-secret",
    MATRIX_USER_ID_FORMAT="username",
)


def _admin_flag(admin):
    """The admin API's answer about the account, asked before a password is set."""
    return respx.get(ADMIN_USER).mock(
        return_value=httpx.Response(200, json={"admin": admin, "locked": False})
    )


def _profile(username="alice", provisioned=True):
    return models.MatrixUserProfile.objects.create(
        user=structure_factories.UserFactory(username=username),
        matrix_user_id=f"@{username}:matrix.example.com",
        provisioned=provisioned,
    )


@override_config(**MATRIX)
class RegistrationPasswordTest(TestCase):
    def _register(self, user):
        route = respx.post(REGISTER).mock(
            side_effect=[
                httpx.Response(401, json={"session": "uia", "flows": []}),
                httpx.Response(400, json={"errcode": "M_EXCLUSIVE"}),
                httpx.Response(
                    401,
                    json={
                        "session": "uia",
                        "flows": [{"stages": ["m.login.dummy"]}],
                    },
                ),
                httpx.Response(200, json={"user_id": "@alice:matrix.example.com"}),
            ]
        )
        matrix_client.ensure_user_exists(user)
        bodies = [json.loads(call.request.content) for call in route.calls]
        self.assertEqual(len(bodies), 4)
        return [body.get("password") for body in bodies]

    @respx.mock
    @mock.patch.object(matrix_client, "set_display_name")
    def test_every_registration_request_carries_one_random_password(
        self, mock_display_name
    ):
        # Tuwunel treats an account without a password as deactivated and
        # refuses single sign-on into it.
        user = structure_factories.UserFactory(username="alice")

        passwords = self._register(user)

        self.assertGreaterEqual(len(passwords[0]), 32)
        self.assertEqual(set(passwords), {passwords[0]})

    @respx.mock
    @mock.patch.object(matrix_client, "set_display_name")
    def test_the_registration_token_flow_carries_it_in_both_requests(
        self, mock_display_name
    ):
        # The second request is the one that creates the account.
        route = respx.post(REGISTER).mock(
            side_effect=[
                httpx.Response(
                    401,
                    json={
                        "session": "uia",
                        "flows": [{"stages": ["m.login.registration_token"]}],
                    },
                ),
                httpx.Response(200, json={"user_id": "@alice:matrix.example.com"}),
            ]
        )

        matrix_client.ensure_user_exists(
            structure_factories.UserFactory(username="alice")
        )

        first, second = (json.loads(call.request.content) for call in route.calls)
        self.assertEqual(second["auth"]["type"], "m.login.registration_token")
        self.assertGreaterEqual(len(second["password"]), 32)
        self.assertEqual(first["password"], second["password"])

    @mock.patch.object(matrix_client, "set_display_name")
    def test_the_password_is_not_derived_from_anything_waldur_keeps(
        self, mock_display_name
    ):
        # It used to be an HMAC of the registration secret and the user's
        # UUID, so the secret unlocked every account.
        user = structure_factories.UserFactory(username="alice")
        with respx.mock:
            first = self._register(user)[0]
        models.MatrixUserProfile.objects.filter(user=user).update(provisioned=False)
        with respx.mock:
            second = self._register(user)[0]

        self.assertNotEqual(first, second)


@override_config(**MATRIX)
class SetPasswordTest(TestCase):
    @respx.mock
    def test_a_homeserver_admins_password_is_never_set(self):
        # A user linked to the admin's account before that was refused would
        # take the homeserver over with it.
        _admin_flag(True)
        route = respx.post(RESET).mock(return_value=httpx.Response(200, json={}))

        with self.assertRaises(matrix_client.MatrixAccountIsHomeserverAdmin):
            matrix_client.set_password("@alice:matrix.example.com", "x" * 32)

        self.assertFalse(route.called)

    @respx.mock
    def test_no_password_while_the_homeserver_fails_to_say_who_is_an_admin(self):
        # The write that follows could succeed, on an admin's account.
        route = respx.post(RESET).mock(return_value=httpx.Response(200, json={}))
        for answer in (
            httpx.Response(502, text="Bad Gateway"),
            httpx.Response(200, json={"name": "@alice:matrix.example.com"}),
            httpx.Response(200, json={"admin": None}),
            httpx.Response(200, text="<html>", headers={"content-type": "text/html"}),
        ):
            respx.get(ADMIN_USER).mock(return_value=answer)

            with self.assertRaises(matrix_client.MatrixClientError) as cm:
                matrix_client.set_password("@alice:matrix.example.com", "x" * 32)

            # Worth a retry, unlike a bot without admin rights.
            self.assertNotIsInstance(cm.exception, matrix_client.MatrixAdminRequired)
        self.assertFalse(route.called)

    @respx.mock
    def test_sets_the_password_through_the_admin_api_as_the_bot(self):
        check = _admin_flag(False)
        route = respx.post(RESET).mock(return_value=httpx.Response(200, json={}))

        matrix_client.set_password("@alice:matrix.example.com", "new-password")

        self.assertTrue(check.called)
        request = route.calls.last.request
        self.assertEqual(request.headers["Authorization"], "Bearer test-as-token")
        # Existing sessions stay signed in.
        self.assertEqual(
            json.loads(request.content),
            {"new_password": "new-password", "logout_devices": False},
        )

    @respx.mock
    def test_signs_out_every_device_when_asked_to(self):
        _admin_flag(False)
        route = respx.post(RESET).mock(return_value=httpx.Response(200, json={}))

        matrix_client.set_password(
            "@alice:matrix.example.com", "new-password", logout_devices=True
        )

        self.assertIs(
            json.loads(route.calls.last.request.content)["logout_devices"], True
        )

    @respx.mock
    def test_a_matrix_id_with_a_slash_stays_one_path_segment(self):
        # Generated localparts may contain "/" and "..", which would otherwise
        # address another admin endpoint.
        check = respx.get(
            url__startswith=f"{HOMESERVER}/_synapse/admin/v2/users/"
        ).mock(return_value=httpx.Response(200, json={"admin": False}))
        route = respx.post(
            url__startswith=f"{HOMESERVER}/_synapse/admin/v1/reset_password/"
        ).mock(return_value=httpx.Response(200, json={}))

        matrix_client.set_password("@a/../b:matrix.example.com", "x" * 32)

        self.assertEqual(
            check.calls.last.request.url.raw_path,
            b"/_synapse/admin/v2/users/%40a%2F..%2Fb%3Amatrix.example.com",
        )
        self.assertEqual(
            route.calls.last.request.url.raw_path,
            b"/_synapse/admin/v1/reset_password/%40a%2F..%2Fb%3Amatrix.example.com",
        )

    @respx.mock
    def test_a_bot_that_is_not_an_admin_is_told_apart(self):
        # Refused at the question already, so the password is not tried.
        respx.get(ADMIN_USER).mock(
            return_value=httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
        )
        route = respx.post(RESET).mock(return_value=httpx.Response(200, json={}))

        with self.assertRaises(matrix_client.MatrixAdminRequired):
            matrix_client.set_password("@alice:matrix.example.com", "x" * 32)

        self.assertFalse(route.called)

    @respx.mock
    def test_a_password_the_homeserver_refuses_to_a_bot_that_is_no_admin(self):
        # The bot lost its admin rights between the two calls.
        _admin_flag(False)
        respx.post(RESET).mock(
            return_value=httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
        )

        with self.assertRaises(matrix_client.MatrixAdminRequired):
            matrix_client.set_password("@alice:matrix.example.com", "x" * 32)

    @respx.mock
    def test_an_account_the_homeserver_does_not_have_is_told_apart(self):
        # The admin API's answer for an ID it has no account for, which no
        # retry changes.
        respx.get(ADMIN_USER).mock(
            return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
        )
        route = respx.post(RESET).mock(return_value=httpx.Response(200, json={}))

        with self.assertRaises(matrix_client.MatrixUserNotFound):
            matrix_client.set_password("@alice:matrix.example.com", "x" * 32)

        self.assertFalse(route.called)

    @respx.mock
    def test_an_account_deleted_before_the_password_is_set_is_told_apart(self):
        _admin_flag(False)
        respx.post(RESET).mock(
            return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
        )

        with self.assertRaises(matrix_client.MatrixUserNotFound):
            matrix_client.set_password("@alice:matrix.example.com", "x" * 32)

    @respx.mock
    def test_other_failures_are_client_errors(self):
        _admin_flag(False)
        respx.post(RESET).mock(return_value=httpx.Response(502, text="Bad Gateway"))

        with self.assertRaises(matrix_client.MatrixClientError) as cm:
            matrix_client.set_password("@alice:matrix.example.com", "x" * 32)
        self.assertNotIsInstance(cm.exception, matrix_client.MatrixAdminRequired)

    @respx.mock
    def test_a_homeserver_without_the_admin_api_is_told_apart_too(self):
        # Blocked by a proxy, or not supported: an operator has to act, as for
        # a bot that is no admin. A missing user is M_NOT_FOUND, tested above.
        route = respx.post(RESET).mock(return_value=httpx.Response(200, json={}))
        for response in (
            httpx.Response(404, json={"errcode": "M_UNRECOGNIZED"}),
            httpx.Response(404, text="<html>Not Found</html>"),
            httpx.Response(405, json={"errcode": "M_UNRECOGNIZED"}),
        ):
            with self.subTest(response=response):
                respx.get(ADMIN_USER).mock(return_value=response)

                with self.assertRaises(matrix_client.MatrixAdminRequired):
                    matrix_client.set_password("@alice:matrix.example.com", "x" * 32)
        self.assertFalse(route.called)

    @respx.mock
    def test_the_bots_password_is_never_set(self):
        route = respx.route(url__startswith=f"{HOMESERVER}/_synapse/admin/").mock(
            return_value=httpx.Response(200, json={})
        )

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.set_password("@waldur-bot:matrix.example.com", "x" * 32)

        self.assertFalse(route.called)


@override_config(**MATRIX, MATRIX_EXTERNAL_LOGIN_METHOD="password")
@mock.patch("waldur_mastermind.matrix_chat.views.matrix_client.set_password")
@mock.patch("waldur_mastermind.matrix_chat.views.matrix_client.ensure_user_exists")
class GeneratePasswordViewTest(test.APITestCase):
    url = "/api/matrix/credentials/password/"

    def setUp(self):
        self.profile = _profile()
        self.client.force_authenticate(self.profile.user)

    def test_generates_a_password_shown_once(self, mock_ensure, mock_set):
        mock_ensure.return_value = self.profile.matrix_user_id

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        password = response.data["password"]
        self.assertGreaterEqual(len(password), 32)
        mock_set.assert_called_once_with(self.profile.matrix_user_id, password)
        self.assertEqual(response.data["matrix_user_id"], self.profile.matrix_user_id)
        self.assertEqual(response.data["homeserver_url"], HOMESERVER)
        self.assertIn("no-store", response["Cache-Control"])

    def test_generating_again_rotates_it(self, mock_ensure, mock_set):
        first = self.client.post(self.url).data["password"]
        second = self.client.post(self.url).data["password"]

        self.assertNotEqual(first, second)

    def test_refused_unless_passwords_are_in_use(self, mock_ensure, mock_set):
        for method in ("none", "oidc"):
            with self.subTest(method=method):
                with override_config(MATRIX_EXTERNAL_LOGIN_METHOD=method):
                    response = self.client.post(self.url)

                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        mock_set.assert_not_called()

    def test_hidden_while_chat_is_off(self, mock_ensure, mock_set):
        with override_config(MATRIX_ENABLED=False):
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_bot_without_admin_rights_is_unavailable(self, mock_ensure, mock_set):
        mock_set.side_effect = matrix_client.MatrixAdminRequired("403")

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertNotIn("password", response.data)
        # Trying again later cannot help until an operator acts.
        self.assertEqual(
            response.data["detail"],
            "Matrix passwords are not available yet; ask your administrator.",
        )

    def test_homeserver_failures_are_unavailable(self, mock_ensure, mock_set):
        mock_set.side_effect = matrix_client.MatrixClientError("unreachable")

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(
            response.data["detail"],
            "Chat is unavailable right now. Please try again later.",
        )

    def test_generating_is_recorded_in_the_event_log(self, mock_ensure, mock_set):
        mock_ensure.return_value = self.profile.matrix_user_id

        password = self.client.post(self.url).data["password"]

        event = Event.objects.get(event_type=EventType.MATRIX_PASSWORD_GENERATED)
        self.assertIn(self.profile.user.username, event.message)
        self.assertNotIn(password, f"{event.message} {event.context}")
        # On the user's own feed.
        self.assertEqual(
            [feed.scope for feed in Feed.objects.filter(event=event)],
            [self.profile.user],
        )

    def test_a_refused_request_is_not_recorded(self, mock_ensure, mock_set):
        mock_set.side_effect = matrix_client.MatrixAdminRequired("403")

        self.client.post(self.url)

        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.MATRIX_PASSWORD_GENERATED
            ).exists()
        )

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks.end_matrix_access")
    def test_a_password_racing_a_deactivation_ends_in_a_locked_account(
        self, mock_end, mock_ensure, mock_set
    ):
        # The deactivation's task may have run before this request provisioned
        # the account, and found nothing to lock.
        user = self.profile.user

        def deactivated_meanwhile(*args, **kwargs):
            type(user).objects.filter(pk=user.pk).update(is_active=False)

        mock_ensure.return_value = self.profile.matrix_user_id
        mock_set.side_effect = deactivated_meanwhile

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn("password", response.data)
        mock_end.delay.assert_called_once_with(user.uuid.hex)
        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.MATRIX_PASSWORD_GENERATED
            ).exists()
        )

    def test_anonymous_is_refused(self, mock_ensure, mock_set):
        self.client.logout()

        self.assertEqual(
            self.client.post(self.url).status_code, status.HTTP_401_UNAUTHORIZED
        )


@override_config(**MATRIX, MATRIX_EXTERNAL_LOGIN_METHOD="password")
@mock.patch("waldur_mastermind.matrix_chat.views.matrix_client.ensure_user_exists")
class GeneratePasswordForAdminAccountTest(test.APITestCase):
    @respx.mock
    def test_a_user_linked_to_a_homeserver_admin_gets_no_password_for_it(
        self, mock_ensure
    ):
        # The takeover: Waldur user `hsadmin` maps to @hsadmin, the homeserver
        # admin, generates a password and signs in as the admin.
        profile = _profile()
        mock_ensure.return_value = profile.matrix_user_id
        _admin_flag(True)
        reset = respx.post(RESET).mock(return_value=httpx.Response(200, json={}))
        self.client.force_authenticate(profile.user)

        response = self.client.post("/api/matrix/credentials/password/")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertNotIn("password", response.data)
        self.assertEqual(
            response.data["detail"],
            "Matrix passwords are not available yet; ask your administrator.",
        )
        self.assertFalse(reset.called)
        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.MATRIX_PASSWORD_GENERATED
            ).exists()
        )

    @respx.mock
    def test_a_failed_admin_check_sets_no_password_either(self, mock_ensure):
        # One 502 on the question must not let the write through.
        profile = _profile()
        mock_ensure.return_value = profile.matrix_user_id
        respx.get(ADMIN_USER).mock(return_value=httpx.Response(502, text="Bad Gateway"))
        reset = respx.post(RESET).mock(return_value=httpx.Response(200, json={}))
        self.client.force_authenticate(profile.user)

        response = self.client.post("/api/matrix/credentials/password/")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(
            response.data["detail"],
            "Chat is unavailable right now. Please try again later.",
        )
        self.assertFalse(reset.called)


@override_config(**MATRIX)
class BotAdminDiagnosticsTest(test.APITestCase):
    def setUp(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

    def _check(self, admin_status, bot_user_id="@waldur-bot:matrix.example.com"):
        self.urls = []

        def get(url, **kwargs):
            self.urls.append(url)
            if url.endswith("/account/whoami"):
                return httpx.Response(200, json={"user_id": bot_user_id})
            if "/_synapse/admin/v2/users/" in url:
                return httpx.Response(admin_status, json={})
            return httpx.Response(200, json={})

        with mock.patch(
            "waldur_mastermind.matrix_chat.views.httpx.get", side_effect=get
        ):
            response = self.client.get("/api/admin/matrix/diagnostics/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {c["name"]: c for c in response.data["checks"]}["bot_homeserver_admin"]

    def test_passes_when_the_bot_is_an_admin(self):
        self.assertTrue(self._check(200)["ok"])

    def test_names_the_fix_when_the_bot_is_not_an_admin(self):
        check = self._check(403)

        self.assertFalse(check["ok"])
        self.assertIn("make-user-admin @waldur-bot:matrix.example.com", check["detail"])
        # What stops working without it.
        self.assertIn("lock", check["detail"])
        self.assertIn("passwords", check["detail"])

    def test_tells_a_blocked_admin_api_apart(self):
        for admin_status in (404, 405):
            with self.subTest(admin_status=admin_status):
                check = self._check(admin_status)

                self.assertFalse(check["ok"])
                self.assertIn("not reachable", check["detail"])

    def test_a_bot_id_with_a_slash_stays_one_path_segment(self):
        self._check(200, bot_user_id="@waldur/bot:matrix.example.com")

        self.assertIn(
            f"{HOMESERVER}/_synapse/admin/v2/users/%40waldur%2Fbot%3Amatrix.example.com",
            self.urls,
        )

    def test_checked_in_every_login_mode(self):
        # Locking the accounts of deactivated users needs it whatever the mode.
        for method in ("none", "password", "oidc"):
            with self.subTest(method=method):
                with override_config(MATRIX_EXTERNAL_LOGIN_METHOD=method):
                    self.assertFalse(self._check(403)["ok"])
